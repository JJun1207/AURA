import json
import re
import runpy
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from aura.apps.telegram import collect
from aura.apps.telegram import adapter
from aura.apps.telegram import collector
from aura.apps.telegram.adapter import TelegramDeviceAdapter
from aura.journal import EventJournal
from aura.models import AcquisitionStatus, Outcome, OutcomeStatus, Route
from aura.profiles import ProfileStore
from aura.runtime import AcquisitionRuntime
from aura.ui import UiRuntime
from fakes import FakeDevice


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ProfileStore(ROOT / "profiles").load_app(
    "telegram", "12.9.2"
)
SYSTEM_UI = ProfileStore(ROOT / "profiles").load_system_ui("samsung")


class Matcher:
    def __init__(self):
        self.adapter = TelegramDeviceAdapter(
            SimpleNamespace(app_profile=PROFILE)
        )

    def matching_elements(self, root, element_id):
        return self.adapter.matching_elements(root, element_id)


MATCHER = Matcher()


@pytest.mark.parametrize(
    "changed",
    [
        (("/sdcard/Download/a.bin", 1, 2),),
        (
            ("/sdcard/Download/a.bin", 1, 2),
            ("/sdcard/Download/b.bin", 3, 4),
        ),
    ],
)
def test_inventory_change_accepts_first_changed_set(changed):
    samples = iter(((), changed))
    assert adapter._await_inventory_change(
        lambda: next(samples), (), 3, 0, "download"
    ) == changed


def test_inventory_change_rejects_repeated_empty_change():
    with pytest.raises(
        adapter.TelegramAdapterError,
        match="download inventory did not change",
    ):
        adapter._await_inventory_change(lambda: (), (), 3, 0, "download")


def test_external_snapshot_batches_stat_calls():
    command = adapter._external_snapshot_command((
        PurePosixPath("/sdcard/Download"),
    ))

    assert "-exec stat -c '%n\t%s\t%Y' {} +" in command
    assert "sh -c" not in command


def test_device_temporal_anchor_uses_device_clock_and_offset():
    commands = []
    actions = []

    class Device:
        @staticmethod
        def shell(command):
            commands.append(command)
            return "2026-07-28T15:00:00+0900\n"

    class Journal:
        @staticmethod
        def record_action(action_id, **kwargs):
            actions.append((action_id, kwargs))
            return SimpleNamespace(action=action_id)

    telegram = TelegramDeviceAdapter(SimpleNamespace(
        app_profile=PROFILE,
        device=Device(),
        journal=Journal(),
    ))

    anchor = telegram.device_temporal_anchor(
        "action-device-time-000001",
        "observation-000001",
    )

    assert anchor == "2026-07-28T15:00:00+09:00"
    assert commands == ["date +%Y-%m-%dT%H:%M:%S%z"]
    assert actions == [(
        "action-device-time-000001",
        {
            "status": "success",
            "context": {
                "before_observation_id": "observation-000001",
            },
            "details": {
                "device_temporal_anchor":
                    "2026-07-28T15:00:00+09:00",
            },
        },
    )]


def default_list_xml(
    *,
    rows=(),
    title="Telegram",
    story_label=None,
    include_profile=False,
):
    navigation_labels = (
        "Chats",
        "Contacts",
        "Settings",
        *(("Profile",) if include_profile else ()),
    )
    row_nodes = "".join(
        '<node package="org.telegram.messenger" '
        'class="android.view.ViewGroup" '
        f'text="{item.get("text", "")}" '
        f'content-desc="{item.get("content_desc", "")}" '
        f'focusable="{str(item.get("focusable", True)).lower()}" '
        f'bounds="{item["bounds"]}" visible-to-user="true"/>'
        for item in rows
    )
    navigation = "".join(
        '<node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" '
        f'bounds="[{59 + index * 260},2076][{282 + index * 260},2220]" '
        'clickable="true" focusable="true" visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        'class="android.widget.TextView" '
        f'text="{label}" bounds="[{59 + index * 260},2161]'
        f'[{282 + index * 260},2210]" visible-to-user="true"/>'
        "</node>"
        for index, label in enumerate(navigation_labels)
    )
    story = (
        '<node package="org.telegram.messenger" '
        'class="android.widget.TextView" '
        f'text="{story_label}" bounds="[222,416][426,455]" '
        'visible-to-user="true"/>'
        if story_label is not None
        else ""
    )
    header = (
        '<node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" bounds="[12,0][1080,248]" '
        'visible-to-user="true"><node package="org.telegram.messenger" '
        'class="android.widget.TextView" '
        f'text="{title}" bounds="[66,106][756,197]" '
        'visible-to-user="true"/></node>'
        if title is not None
        else (
            '<node package="org.telegram.messenger" '
            'class="android.widget.FrameLayout" bounds="[21,242][1059,386]" '
            'visible-to-user="true"><node package="org.telegram.messenger" '
            'class="android.widget.EditText" text="Search Chats" '
            'content-desc="Search Chats" bounds="[33,254][1047,374]" '
            'visible-to-user="true"/></node>'
        )
    )
    return (
        '<hierarchy><node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" '
        'visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        'class="androidx.recyclerview.widget.RecyclerView" scrollable="true" '
        'bounds="[0,0][1080,2400]" visible-to-user="true">'
        f"{row_nodes}</node>"
        f"{header}"
        f"{story}"
        f"{navigation}</node></hierarchy>"
    )


def message_page_xml(raw="Photo&#10;2:18 PM", *, go_to_bottom=False):
    go_to_bottom_node = (
        '<node package="org.telegram.messenger" '
        'class="android.widget.ImageView" content-desc="Go to bottom" '
        'clickable="true" focusable="true" bounds="[909,1893][1077,2085]" '
        'visible-to-user="true"/>'
        if go_to_bottom
        else ""
    )
    return (
        '<hierarchy><node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" '
        'visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        'class="androidx.recyclerview.widget.RecyclerView" scrollable="true" '
        'bounds="[0,0][1080,2400]" visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        'class="android.view.ViewGroup" '
        f'text="{raw}" focusable="true" clickable="false" '
        'bounds="[0,500][1080,800]" visible-to-user="true"/></node>'
        '<node package="org.telegram.messenger" '
        'class="android.widget.ImageView" content-desc="Go back" '
        'clickable="true" focusable="true" bounds="[0,80][162,248]" '
        'visible-to-user="true"/>'
        '<node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" clickable="true" focusable="true" '
        'bounds="[156,0][924,248]" visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        'class="android.widget.TextView" text="Synthetic Chat" '
        'bounds="[168,80][800,160]" visible-to-user="true"/></node>'
        f"{go_to_bottom_node}"
        '<node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" bounds="[0,2076][1080,2400]" '
        'visible-to-user="true"><node package="org.telegram.messenger" '
        'class="android.view.View" content-desc="Message" clickable="true" '
        'focusable="true" bounds="[21,2097][1059,2229]" '
        'visible-to-user="true"/></node>'
        "</node></hierarchy>"
    )


def container_info_xml(
    *,
    display_name="Synthetic Chat",
    type_label="Direct",
    username="synthetic_handle",
    mobile=None,
    nested_scrollable=False,
):
    mobile_node = (
        '<node package="org.telegram.messenger" '
        f'class="android.widget.TextView" text="Mobile: {mobile}" '
        'bounds="[120,560][960,640]" visible-to-user="true"/>'
        if mobile is not None
        else ""
    )
    nested_node = (
        '<node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" bounds="[0,800][1080,2400]" '
        'visible-to-user="true"><node package="org.telegram.messenger" '
        'class="androidx.recyclerview.widget.RecyclerView" '
        'bounds="[0,900][1080,2400]" scrollable="true" '
        'visible-to-user="true"/></node>'
        if nested_scrollable
        else ""
    )
    return (
        '<hierarchy><node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" '
        'visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        'class="android.widget.ImageView" content-desc="Go back" '
        'clickable="true" focusable="true" bounds="[6,80][168,248]" '
        'visible-to-user="true"/>'
        '<node package="org.telegram.messenger" '
        'class="androidx.recyclerview.widget.RecyclerView" '
        'bounds="[0,248][1080,2400]" scrollable="true" '
        'visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        f'class="android.widget.TextView" text="{display_name}" '
        'bounds="[120,320][960,400]" visible-to-user="true"/>'
        '<node package="org.telegram.messenger" '
        f'class="android.widget.TextView" text="{type_label}" '
        'bounds="[120,400][960,480]" visible-to-user="true"/>'
        '<node package="org.telegram.messenger" '
        f'class="android.widget.TextView" text="Username: {username}" '
        'bounds="[120,480][960,560]" visible-to-user="true"/>'
        f"{mobile_node}{nested_node}</node></node></hierarchy>"
    )


def account_profile_xml(
    *,
    display_name="Example User",
    mobile="+1 202-555-0100",
    username="@example_user",
    field_attribute="content-desc",
):
    return (
        '<hierarchy><node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" '
        'visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        'class="android.widget.ImageView" content-desc="QR Code" '
        'bounds="[6,80][168,248]" visible-to-user="true"/>'
        '<node package="org.telegram.messenger" '
        'class="android.widget.TextView" '
        f'text="{display_name}" bounds="[408,504][976,607]" '
        'visible-to-user="true"/>'
        '<node package="org.telegram.messenger" '
        'class="android.widget.TextView" text="online" '
        'bounds="[485,596][1080,656]" visible-to-user="true"/>'
        '<node package="org.telegram.messenger" '
        'class="android.widget.Button" text="Edit Info" '
        'bounds="[381,728][699,890]" clickable="true" '
        'visible-to-user="true"/>'
        '<node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" '
        f'{field_attribute}="Mobile: {mobile}" '
        'bounds="[36,944][1044,1124]" '
        'visible-to-user="true"/>'
        '<node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" '
        f'{field_attribute}="Username: {username}" '
        'bounds="[36,1124][1044,1306]" visible-to-user="true"/>'
        '<node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" bounds="[59,2076][282,2220]" '
        'clickable="true" focusable="true" visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        'class="android.widget.TextView" text="Chats" '
        'bounds="[59,2161][282,2210]" visible-to-user="true"/>'
        "</node>"
        '<node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" bounds="[786,2076][1020,2220]" '
        'clickable="true" focusable="true" visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        'class="android.widget.TextView" text="Profile" '
        'bounds="[786,2161][1020,2210]" visible-to-user="true"/>'
        "</node></node></hierarchy>"
    )


def menu_xml(label="Save to Gallery"):
    return (
        '<hierarchy><node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" '
        'visible-to-user="true"><node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" clickable="true" '
        'bounds="[500,800][1080,1040]" visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        f'class="android.widget.TextView" text="{label}" '
        'bounds="[540,850][1030,990]" visible-to-user="true"/>'
        "</node></node></hierarchy>"
    )


def loading_dialog_xml(percent="0%"):
    return (
        '<hierarchy><node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" '
        'visible-to-user="true"><node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" bounds="[66,1030][1014,1524]" '
        'visible-to-user="true"><node package="org.telegram.messenger" '
        'class="android.widget.TextView" text="Loading..." '
        'bounds="[130,1104][948,1163]" visible-to-user="true"/>'
        '<node package="org.telegram.messenger" class="android.view.View" '
        'bounds="[130,1360][946,1370]" visible-to-user="true"/>'
        '<node package="org.telegram.messenger" '
        f'class="android.widget.TextView" text="{percent}" '
        'bounds="[130,1400][946,1452]" visible-to-user="true"/>'
        "</node></node></hierarchy>"
    )


def test_clickable_ancestor_bounds_uses_nearest_menu_item():
    root = ET.fromstring(
        '<hierarchy><node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" clickable="true" '
        'bounds="[217,416][817,1436]" visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        'class="android.widget.FrameLayout" clickable="true" '
        'bounds="[217,716][817,860]" visible-to-user="true">'
        '<node package="org.telegram.messenger" '
        'class="android.widget.TextView" text="Save to Gallery" '
        'clickable="false" bounds="[271,755][717,820]" '
        'visible-to-user="true"/>'
        "</node></node></hierarchy>"
    )
    label = next(
        node
        for node in root.iter("node")
        if node.get("text") == "Save to Gallery"
    )

    assert collector._clickable_ancestor_bounds(root, label) == (
        217, 716, 817, 860
    )


def parsed_metadata(**kwargs):
    root = ET.fromstring(container_info_xml(**kwargs))
    page = collector._container_info_page(MATCHER, root)
    assert page is not None
    return collector._parse_container_metadata(MATCHER.adapter, page)


def test_logical_chatroom_id_uses_account_and_stable_metadata_not_name():
    first = parsed_metadata(username="first_handle")
    same = parsed_metadata(
        display_name="Renamed Chat",
        username="first_handle",
    )
    second = parsed_metadata(username="second_handle")

    first_id = collector._logical_chatroom_id(
        MATCHER.adapter,
        "account-a",
        first,
    )

    assert first_id == collector._logical_chatroom_id(
        MATCHER.adapter,
        "account-a",
        same,
    )
    assert first_id != collector._logical_chatroom_id(
        MATCHER.adapter,
        "account-a",
        second,
    )
    assert first_id != collector._logical_chatroom_id(
        MATCHER.adapter,
        "account-b",
        first,
    )
    assert re.fullmatch(r"telegram-chat-[0-9a-f]{64}", first_id)


def test_metadata_fingerprint_normalizes_complete_member_order():
    base = {
        "type_label": "Group",
        "username": None,
        "mobile": None,
        "members_complete": True,
    }
    first = collector._metadata_fingerprint(
        MATCHER.adapter,
        {**base, "member_labels": [" Bob ", "Ａlice"]},
        "group",
    )
    second = collector._metadata_fingerprint(
        MATCHER.adapter,
        {**base, "member_labels": ["Alice", "Bob"]},
        "group",
    )

    assert first == second
    assert first[1] == ["type_label", "member_labels"]


def test_ambiguous_metadata_fingerprint_uses_observed_name_and_counts():
    base = {
        "type_label": None,
        "username": None,
        "mobile": None,
        "member_labels": [],
        "members_complete": False,
        "member_count": None,
    }

    first = collector._metadata_fingerprint(
        MATCHER.adapter,
        {**base, "display_name": "Test2", "subscriber_count": 2},
        "channel",
    )
    second = collector._metadata_fingerprint(
        MATCHER.adapter,
        {**base, "display_name": "Minsu Kim", "subscriber_count": 1},
        "channel",
    )

    assert first != second
    assert first[1] == ["display_name", "subscriber_count"]


def test_ambiguous_chatroom_revisit_uses_metadata_not_volatile_row_text():
    logical_chatroom_id = "telegram-chat-" + "a" * 64

    assert collector._chatroom_revisit_key(
        logical_chatroom_id, "identified"
    ) == logical_chatroom_id
    assert collector._chatroom_revisit_key(
        logical_chatroom_id, "ambiguous"
    ) == logical_chatroom_id
    assert collector._chatroom_revisit_key(
        logical_chatroom_id, "insufficient"
    ) is None


def test_ambiguous_chatroom_outputs_are_scoped_by_container():
    outputs = adapter.TelegramOutputs(object(), object(), "target-ref")
    logical_chatroom_id = "telegram-chat-" + "a" * 64

    first = outputs.for_chatroom(
        logical_chatroom_id,
        "Same Name",
        container_ref="container-000001",
        identity_status="ambiguous",
    )
    second = outputs.for_chatroom(
        logical_chatroom_id,
        "Same Name",
        container_ref="container-000002",
        identity_status="ambiguous",
    )

    assert first.relative_root != second.relative_root
    assert first.relative_root.endswith("[container-000001]")
    assert second.relative_root.endswith("[container-000002]")


def test_default_list_rows_use_visible_hierarchy_bounds():
    rows = (
        {
            "bounds": "[0,200][1080,563]",
            "content_desc": "Alice, hello",
        },
        {"bounds": "[0,563][1080,774]"},
    )
    root = ET.fromstring(default_list_xml(rows=rows))
    page = collector._default_list(MATCHER, root)

    occurrences = collector._row_occurrences(page, 1)

    assert page is not None
    assert occurrences["eligible"] == (
        {
            "page_number": 1,
            "literal_slot": 0,
            "rect": (0, 200, 1080, 563),
                "visible_rect": (0, 360, 1080, 563),
            "labeled": True,
            "selector_id": "telegram.chat-row.p0001.s0000",
        },
        {
            "page_number": 1,
            "literal_slot": 1,
            "rect": (0, 563, 1080, 774),
            "visible_rect": (0, 563, 1080, 774),
            "labeled": False,
            "selector_id": "telegram.chat-row.p0001.s0001",
        },
    )
    assert not occurrences["deferred"]
    assert collector._default_list(
        MATCHER, ET.fromstring(default_list_xml(rows=rows, title="Other"))
    ) is None


def test_contacts_section_ends_default_chatroom_rows():
    root = ET.fromstring(default_list_xml(rows=(
        {
            "bounds": "[0,248][1080,459]",
            "content_desc": "Minsu Kim, latest message",
        },
        {
            "bounds": "[0,459][1080,570]",
            "text": "Your contacts on Telegram",
        },
        {
            "bounds": "[0,570][1080,781]",
            "content_desc": "Suggested Contact",
        },
    )))

    page = collector._default_list(MATCHER, root)
    occurrences = collector._row_occurrences(page, 1)

    assert page["end_sentinel_top"] == 459
    assert tuple(
        item["literal_slot"] for item in occurrences["eligible"]
    ) == (0,)
    assert occurrences["excluded"][-2:] == (
        {"literal_slot": 1, "reason": "contacts_section"},
        {"literal_slot": 2, "reason": "contacts_section"},
    )


def test_contacts_section_stops_enumeration_without_another_scroll():
    hierarchy = default_list_xml(rows=({
        "bounds": "[0,459][1080,570]",
        "text": "Your contacts on Telegram",
    },)).encode()

    class ContactsBoundaryDevice:
        stagnation_rounds = 1

        @staticmethod
        def click_bounds(action_id, bounds, observation_id, *, anchor):
            assert action_id.startswith("action-chat-list-top-")

        @staticmethod
        def matching_elements(root, element_id):
            return MATCHER.matching_elements(root, element_id)

        @staticmethod
        def observe(observation_id):
            return {"observation_id": observation_id}

        @staticmethod
        def read_observation(observation_id, kind):
            assert kind == "ui_tree"
            return hierarchy

        @staticmethod
        def scroll(*args, **kwargs):
            raise AssertionError("contacts boundary must stop scrolling")

    report = collector._enumerate_containers(
        ContactsBoundaryDevice(),
        "target-" + "a" * 64,
    )

    assert report["status"] == "complete"
    assert report["bottom_boundary"] == {
        "established": True,
        "observation_id": "observation-000002",
        "reason_code": "contacts_section_reached",
        "stationary_action_ids": ["action-chat-list-top-000001"],
        "stationary_observation_ids": ["observation-000002"],
    }


def test_expected_revisit_and_prior_page_top_clip_are_complete(monkeypatch):
    # This test supplies parsed page dictionaries; top normalization has its
    # own real XML regression, while this covers continuing the scrolled page.
    monkeypatch.setattr(collector, "_establish_list_top", lambda device, state, observation_id, root, page: (observation_id, root, page, None))
    first_page = {"end_sentinel_top": None}
    final_page = {"end_sentinel_top": 500}
    observed = iter((
        ("observation-page-1", first_page, None),
        ("observation-page-2", final_page, None),
    ))
    occurrence = {
        "page_number": 2,
        "literal_slot": 1,
        "rect": (0, 252, 1080, 463),
        "visible_rect": (0, 252, 1080, 463),
    }

    class Device:
        stagnation_rounds = 1

        @staticmethod
        def scroll(*args, **kwargs):
            return True

    monkeypatch.setattr(
        collector, "_observe", lambda device, state: next(observed)
    )
    monkeypatch.setattr(
        collector, "_default_list", lambda device, root: root
    )
    monkeypatch.setattr(
        collector,
        "_acquire_account_profile",
        lambda device, state, observation_id, root, outputs, target_ref: (
            None, observation_id, root, root, None
        ),
    )
    monkeypatch.setattr(
        collector,
        "_safe_occurrences",
        lambda page, page_number: (
            {
                "eligible": (occurrence,),
                "deferred": ({
                    **occurrence,
                    "literal_slot": 0,
                    "edge": "top",
                },),
            }
            if page_number == 2
            else {"eligible": (), "deferred": ()},
            None,
        ),
    )
    monkeypatch.setattr(
        collector,
        "_acquire_scrollable_occurrence",
        lambda *args, **kwargs: (
            {
                "revisit": True,
                "logical_chatroom_id": "telegram-chat-" + "a" * 64,
                "display_name": "Minsu Kim",
            },
            "observation-page-2",
            final_page,
            None,
        ),
    )

    report = collector._enumerate_containers(
        Device(), "account-pseudonym", outputs=object()
    )

    assert report["status"] == "complete"
    assert report["limitations"] == ()
    assert report["deferred"] == ()


def test_default_list_ignores_matching_story_label():
    root = ET.fromstring(default_list_xml(story_label="Telegram"))

    assert collector._default_list(MATCHER, root) is not None


@pytest.mark.parametrize(
    "title",
    ("Telegram", "Waiting for network...", "Connecting..."),
)
def test_default_list_accepts_reviewed_connection_titles(title):
    root = ET.fromstring(default_list_xml(title=title))

    assert collector._default_list(MATCHER, root) is not None


def test_default_list_does_not_click_row_behind_search_surface():
    root = ET.fromstring(default_list_xml(rows=({
        "bounds": "[0,202][1080,413]",
        "content_desc": "Clipped chat row",
    },)))

    page = collector._default_list(MATCHER, root)
    occurrences = collector._row_occurrences(page, 2)

    assert page["viewport"][1] == 360
    assert occurrences["eligible"] == ()
    assert occurrences["deferred"][0]["edge"] == "top"


def test_default_list_falls_back_to_search_when_logo_has_no_label():
    root = ET.fromstring(default_list_xml(title=None))

    page = collector._default_list(MATCHER, root)

    assert page is not None
    assert page["viewport"] == (0, 386, 1080, 2076)


def test_list_geometry_ignores_uniform_header_translation():
    before = {
        "eligible": (
            {"literal_slot": 0, "rect": (0, 392, 1080, 603)},
            {"literal_slot": 2, "rect": (0, 814, 1080, 1025)},
        ),
        "deferred": (),
    }
    after = {
        "eligible": (
            {"literal_slot": 0, "rect": (0, 248, 1080, 459)},
            {"literal_slot": 2, "rect": (0, 670, 1080, 881)},
        ),
        "deferred": (),
    }

    assert collector._geometry(before) == collector._geometry(after)


def test_visible_row_rect_rejects_insufficient_visible_height():
    viewport = (0, 248, 1080, 2076)

    assert collector._visible_row_rect(
        (0, 200, 1080, 563), viewport
    ) == (0, 248, 1080, 563)
    assert collector._visible_row_rect((0, 220, 1080, 270), viewport) is None


def test_archive_is_out_of_scope_for_default_list_enumeration():
    hierarchy = default_list_xml(rows=()).encode()

    class DefaultListDevice:
        stagnation_rounds = 1

        @staticmethod
        def click_bounds(action_id, bounds, observation_id, *, anchor):
            assert action_id.startswith("action-chat-list-top-")

        def __init__(self):
            self.observations = {}

        def matching_elements(self, root, element_id):
            return MATCHER.matching_elements(root, element_id)

        def observe(self, observation_id):
            self.observations[observation_id] = hierarchy
            return {"observation_id": observation_id}

        def read_observation(self, observation_id, kind):
            assert kind == "ui_tree"
            return self.observations[observation_id]

        @staticmethod
        def scroll(action_id, before_observation_id, *, direction):
            assert action_id == "action-scroll-000002"
            assert before_observation_id == "observation-000002"
            assert direction == "forward"
            return False

    report = collector._enumerate_containers(
        DefaultListDevice(),
        "target-" + "a" * 64,
    )

    assert report["status"] == "complete"
    assert report["limitations"] == ()
    assert report["archive"] == {
        "status": "out_of_scope",
        "reason_code": "archived_chatrooms_not_collected",
        "top_boundary": {"established": False},
        "bottom_boundary": {"established": False},
        "occurrence_count": 0,
    }


def test_labelled_archive_row_is_excluded_from_default_list():
    root = ET.fromstring(default_list_xml(rows=(
        {
            "bounds": "[0,248][1080,459]",
            "content_desc": "Archived Chats. 2 unread",
        },
        {
            "bounds": "[0,459][1080,670]",
            "content_desc": "Alpha",
        },
    )))

    occurrences = collector._row_occurrences(
        collector._default_list(MATCHER, root),
        1,
    )

    assert occurrences["excluded"] == ({
        "literal_slot": 0,
        "reason": "archive_surface",
    },)
    assert tuple(
        item["literal_slot"] for item in occurrences["eligible"]
    ) == (1,)


def test_container_info_uses_outer_list_when_bot_has_nested_media_list():
    root = ET.fromstring(container_info_xml(nested_scrollable=True))

    page = collector._container_info_page(MATCHER, root)

    assert page is not None
    assert collector._rect(page["list"]) == (0, 248, 1080, 2400)
    metadata = collector._parse_container_metadata(
        MATCHER.adapter,
        page,
        display_name="Synthetic Chat",
    )
    assert metadata["container_type"] == "direct"
    assert metadata["fingerprint_inputs"] == ["type_label", "username"]


@pytest.mark.parametrize(
    ("header_labels", "raw", "expected_type", "expected_basis"),
    [
        (
            ("8,200,970 monthly users",),
            "/mybots\nSent at 8:47 PM, Seen\n",
            "bot",
            ["header.monthly-users"],
        ),
        (
            ("Service notifications",),
            "Test\nSent at 8:02 PM, Seen\n",
            "service",
            ["header.service-notifications"],
        ),
    ],
)
def test_special_container_cues_enable_message_parsing(
    header_labels, raw, expected_type, expected_basis
):
    observed = {
        "type_label": None,
        "mobile": None,
        "member_count": None,
        "header_labels": header_labels,
    }

    container_type, status, basis = collector._classify_container_type(
        {"cue_ids": ()},
        observed,
    )
    parsed, _ = collector._parse_message_row(raw, container_type, None)

    assert container_type == expected_type
    assert status == "corroborated"
    assert basis == expected_basis
    assert parsed["row_class"] == "message"


def test_bot_description_uses_noninteractive_view_context_shape():
    raw = (
        "What can this bot do?\n\n"
        "BotFather is the one bot to rule them all."
    )

    def parsed_row(class_name, enabled, focusable):
        root = ET.fromstring(
            message_page_xml(raw.replace("\n", "&#10;"))
        )
        message_list = next(
            node for node in root.iter()
            if node.get("class")
            == "androidx.recyclerview.widget.RecyclerView"
        )
        node = list(message_list)[0]
        node.set("class", class_name)
        node.set("enabled", enabled)
        node.set("focusable", focusable)
        page = collector._message_page(MATCHER, root)
        rows, _, reason = collector._message_window(
            page,
            "bot",
            action_id="action-1",
            observation_id="observation-1",
            window_index=0,
            state={"message_occurrences": 0},
        )
        assert reason is None
        return rows[0]

    context = parsed_row("android.view.View", "false", "false")
    ordinary_view = parsed_row(
        "android.view.ViewGroup", "true", "true"
    )

    assert context["row_class"] == "bot_context"
    assert collector._conversation_text([context]) == (
        b"[context] bot-description: What can this bot do?\n\n"
        b"BotFather is the one bot to rule them all.\n"
    )
    assert ordinary_view["row_class"] == "raw"


@pytest.mark.parametrize("field_attribute", ["content-desc", "text"])
def test_account_profile_parser_uses_verified_visible_fields(
    field_attribute,
):
    assert hasattr(collector, "_account_profile_page")
    assert hasattr(collector, "_parse_account_profile")
    root = ET.fromstring(
        account_profile_xml(field_attribute=field_attribute)
    )

    page = collector._account_profile_page(MATCHER, root)

    assert page is not None
    assert collector._parse_account_profile(page) == {
        "display_name": "Example User",
        "mobile": "+1 202-555-0100",
        "username": "@example_user",
    }


def test_account_profile_acquisition_writes_one_evidence_group():
    assert hasattr(collector, "_acquire_account_profile")
    source_xml = default_list_xml(rows=(), include_profile=True)
    source_root = ET.fromstring(source_xml)
    profile_xml = account_profile_xml()

    class Device:
        def __init__(self):
            self.observations = {}
            self.samples = iter((profile_xml, source_xml))
            self.clicks = []

        def matching_elements(self, root, element_id):
            return MATCHER.matching_elements(root, element_id)

        def click_bounds(
            self,
            action_id,
            bounds,
            before_observation_id,
            *,
            anchor="lower_third",
        ):
            self.clicks.append((
                action_id,
                tuple(bounds),
                before_observation_id,
                anchor,
            ))

        def observe(self, observation_id):
            self.observations[observation_id] = next(
                self.samples
            ).encode()
            return {"observation_id": observation_id}

        def read_observation(self, observation_id, kind):
            if kind == "ui_tree":
                return self.observations[observation_id]
            assert observation_id == "observation-000001"
            return b"account-png"

    device = Device()
    outputs = Outputs()
    record, observation_id, _, page, reason = (
        collector._acquire_account_profile(
            device,
            {"actions": 0, "observations": 0},
            "observation-source",
            source_root,
            outputs,
            "target-" + "a" * 64,
        )
    )

    assert reason is None
    assert observation_id == "observation-000002"
    assert page is not None
    assert record["profile"]["display_name"] == "Example User"
    assert device.clicks == [
        (
            "action-account-profile-000001",
            (839, 2076, 1062, 2220),
            "observation-source",
            "center",
        ),
        (
            "action-account-profile-back-000002",
            (59, 2076, 282, 2220),
            "observation-000001",
            "center",
        ),
    ]
    assert [
        item["artifact_id"] for item in outputs.records
    ] == ["account-screen", "account-tree", "account"]
    assert outputs.records[2]["value"]["source"] == {
        "action_id": "action-account-profile-000001",
        "observation_id": "observation-000001",
        "screen_artifact_id": "artifact-000001",
        "screen_path": "artifacts/account-screen",
        "ui_tree_artifact_id": "artifact-000002",
        "ui_tree_path": "artifacts/account-tree",
    }


def test_telegram_adapter_forwards_only_boundary_target(tmp_path):
    class BoundsDevice(FakeDevice):
        def __init__(self):
            super().__init__()
            self.boundary_clicks = []

        def click_bounds(self, bounds, *, anchor="center"):
            self.boundary_clicks.append((tuple(bounds), anchor))

    device = BoundsDevice()
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    context = SimpleNamespace(
        app_profile=PROFILE,
        device=device,
        journal=journal,
        ui=UiRuntime(device, journal),
    )
    adapter = TelegramDeviceAdapter(context)

    adapter.click_bounds(
        "action-enter-000001",
        (0, 248, 1080, 459),
        "observation-list-000001",
    )

    assert device.boundary_clicks == [
        ((0, 248, 1080, 459), "lower_third")
    ]
    assert adapter.actions["action-enter-000001"].status == "success"


def test_transition_action_settles_before_observation(
    monkeypatch,
    tmp_path,
):
    timeline = []
    hierarchy = default_list_xml(rows=())

    class TransitionDevice(FakeDevice):
        def click_bounds(self, bounds, *, anchor="center"):
            timeline.append(("click_bounds", tuple(bounds)))

        def hierarchy(self):
            timeline.append(("hierarchy", None))
            return hierarchy

    class Artifacts:
        @staticmethod
        def write_bytes(path, value, **kwargs):
            return None

    monkeypatch.setattr(
        "aura.apps.telegram.adapter.time.sleep",
        lambda seconds: timeline.append(("settle", seconds)),
    )
    device = TransitionDevice()
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    adapter = TelegramDeviceAdapter(
        SimpleNamespace(
            app_profile=PROFILE,
            device=device,
            journal=journal,
            ui=UiRuntime(device, journal, poll_interval=0),
            artifacts=Artifacts(),
        )
    )

    adapter.click_bounds(
        "action-enter-000001",
        (0, 248, 1080, 459),
        "observation-list-000001",
    )
    adapter.observe("observation-message-000001")

    assert timeline[:3] == [
        ("click_bounds", (0, 248, 1080, 459)),
        ("settle", 0.5),
        ("hierarchy", None),
    ]


def test_telegram_scroll_detects_recycled_rows_with_new_labels(tmp_path):
    before = default_list_xml(
        rows=(
            {
                "bounds": "[0,352][1080,563]",
                "content_desc": "Alice, first message",
            },
        )
    )
    after = default_list_xml(
        rows=(
            {
                "bounds": "[0,352][1080,563]",
                "content_desc": "Bob, second message",
            },
        )
    )
    device = FakeDevice(
        hierarchy_before=before,
        hierarchy_after_swipe=after,
    )
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    adapter = TelegramDeviceAdapter(
        SimpleNamespace(
            app_profile=PROFILE,
            device=device,
            journal=journal,
            ui=UiRuntime(device, journal),
        )
    )

    moved = adapter.scroll(
        "action-scroll-000001",
        "observation-list-000001",
    )

    assert moved is True


def test_telegram_scroll_settles_before_reading_result_hierarchy(
    monkeypatch,
    tmp_path,
):
    timeline = []
    hierarchy = default_list_xml(rows=())

    class Device(FakeDevice):
        def hierarchy(self):
            timeline.append("hierarchy")
            return hierarchy

        def swipe(self, *args, **kwargs):
            timeline.append("swipe")

    monkeypatch.setattr(
        "aura.apps.telegram.adapter.time.sleep",
        lambda seconds: timeline.append(("settle", seconds)),
    )
    device = Device()
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    telegram = TelegramDeviceAdapter(SimpleNamespace(
        app_profile=PROFILE,
        device=device,
        journal=journal,
        ui=UiRuntime(device, journal, poll_interval=0),
    ))

    telegram.scroll(
        "action-scroll-000001",
        "observation-list-000001",
    )

    swipe_index = timeline.index("swipe")
    assert timeline[swipe_index + 1] == ("settle", 0.5)
    assert timeline[swipe_index + 2] == "hierarchy"


def test_telegram_scroll_ignores_transient_overscroll_structure(tmp_path):
    stable = default_list_xml(
        rows=(
            {
                "bounds": "[0,392][1080,603]",
                "content_desc": "Alice, first message",
            },
        )
    )
    transient = default_list_xml(
        rows=(
            {
                "bounds": "[0,410][1080,621]",
                "content_desc": "Alice, first message",
            },
        )
    )

    class OverscrollDevice(FakeDevice):
        def __init__(self):
            super().__init__()
            self.samples = iter((stable, transient, stable, stable))

        def hierarchy(self):
            return next(self.samples)

    device = OverscrollDevice()
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    adapter = TelegramDeviceAdapter(
        SimpleNamespace(
            app_profile=PROFILE,
            device=device,
            journal=journal,
            ui=UiRuntime(device, journal, poll_interval=0),
        )
    )

    moved = adapter.scroll(
        "action-scroll-000001",
        "observation-list-000001",
    )

    assert moved is False


def test_telegram_observation_waits_until_transition_hierarchy_settles(
    tmp_path,
):
    transition = (
        "<hierarchy>"
        '<node package="org.telegram.messenger" scrollable="true"/>'
        '<node package="org.telegram.messenger" scrollable="true"/>'
        "</hierarchy>"
    )
    stable = default_list_xml(rows=())

    class TransitionDevice(FakeDevice):
        def __init__(self):
            super().__init__()
            self.samples = iter((transition, stable, stable))

        def hierarchy(self):
            return next(self.samples)

    class Artifacts:
        def __init__(self):
            self.values = {}

        def write_bytes(self, path, value, **kwargs):
            self.values[path] = value

    device = TransitionDevice()
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    artifacts = Artifacts()
    adapter = TelegramDeviceAdapter(
        SimpleNamespace(
            app_profile=PROFILE,
            device=device,
            journal=journal,
            ui=UiRuntime(device, journal, poll_interval=0),
            artifacts=artifacts,
        )
    )

    adapter.observe("observation-000001")

    assert adapter.read_observation(
        "observation-000001", "ui_tree"
    ) == stable.encode()
    assert artifacts.values == {}
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["event_type"] == "observation_recorded"
    assert events[-1]["context"]["observation_id"] == (
        "observation-000001"
    )


def test_enter_occurrence_uses_boundary_and_records_message_header(
    monkeypatch,
):
    source_xml = default_list_xml(
        rows=(
            {
                "bounds": "[0,352][1080,563]",
                "content_desc": "Synthetic Chat, latest message",
            },
        )
    )
    source_page = collector._default_list(
        MATCHER, ET.fromstring(source_xml)
    )
    occurrence = collector._row_occurrences(source_page, 1)["eligible"][0]

    class NavigationDevice:
        def __init__(self):
            self.observation_xml = iter((
                message_page_xml(),
                container_info_xml(),
                message_page_xml(),
                source_xml,
            ))
            self.observations = {}
            self.boundary_actions = []
            self.semantic_actions = []

        def matching_elements(self, root, element_id):
            return MATCHER.matching_elements(root, element_id)

        @staticmethod
        def element_selector(element_id):
            assert element_id == "telegram.navigation.back"
            return {
                "kind": "content_description",
                "owner_package": "org.telegram.messenger",
                "selector_id": "telegram.navigation.back.content-description",
                "value": "Go back",
                "constraints": {"class_name": "android.widget.ImageView"},
            }

        def click_bounds(
            self, action_id, bounds, before_observation_id, *, anchor="lower_third"
        ):
            self.boundary_actions.append(
                (action_id, tuple(bounds), before_observation_id, anchor)
            )

        def click(self, action_id, selector, before_observation_id):
            self.semantic_actions.append(
                (action_id, selector["selector_id"], before_observation_id)
            )

        def observe(self, observation_id):
            self.observations[observation_id] = next(
                self.observation_xml
            ).encode()
            return {"observation_id": observation_id}

        def read_observation(self, observation_id, kind):
            if kind == "screen_image":
                return b"png"
            return self.observations[observation_id]

        @staticmethod
        def canonical_sha256(value):
            return TelegramDeviceAdapter.canonical_sha256(value)

    device = NavigationDevice()
    state = {
        "actions": 0,
        "observations": 0,
        "occurrences": 0,
        "containers": 0,
    }
    def fake_write_single(device, scoped_outputs, **kwargs):
        scoped_outputs.write_json(
            artifact_id="message-ui-000001",
            value={"record_kind": "message"},
        )
        return {"message_ref": "message-occurrence-000001"}, None

    monkeypatch.setattr(
        collector, "_write_single_text_message", fake_write_single
    )

    outputs = Outputs()
    record, _, _, reason = collector._enter_occurrence(
        device,
        state,
        occurrence,
        "observation-source",
        target_ref="account-a",
        outputs=outputs,
    )

    assert reason is None
    assert device.boundary_actions == [
        (
            "action-click-000001",
                (0, 360, 1080, 563),
            "observation-source",
            "lower_third",
        ),
        (
            "action-container-info-000002",
            (156, 0, 924, 248),
            "observation-000001",
            "center",
        ),
    ]
    assert device.semantic_actions == [
        (
            "action-container-info-back-000003",
            "telegram.navigation.back.content-description",
            "observation-000002",
        ),
        (
            "action-back-000004",
            "telegram.navigation.back.content-description",
            "observation-000003",
        ),
    ]
    assert record["display_name"] == "Synthetic Chat"
    assert record["visible_rect"] == (0, 360, 1080, 563)
    assert re.fullmatch(
        r"telegram-chat-[0-9a-f]{64}",
        record["logical_chatroom_id"],
    )
    assert record["container_ref"] == "container-000001"
    assert record["identity_method"] == "telegram-chatroom-v1"
    assert record["identity_inputs"] == ["type_label", "username"]
    assert record["identity_status"] == "identified"
    assert record["container_type"] == "direct"
    assert record["type_status"] == "explicit"
    assert record["metadata_linkage"] == {
        "source_observation_id": "observation-000001",
        "entry_action_id": "action-container-info-000002",
        "info_observation_id": "observation-000002",
        "return_action_id": "action-container-info-back-000003",
        "return_observation_id": "observation-000003",
    }
    assert outputs.scopes == [(
        record["logical_chatroom_id"],
        "Synthetic Chat",
    )]
    assert [
        item["artifact_id"] for item in outputs.records
    ] == [
        "chatroom-screen",
        "chatroom-tree",
        "chatroom",
        "message-ui-000001",
    ]


def test_acquire_occurrence_uses_boundary_and_retains_identity(monkeypatch):
    occurrence = {
        "visible_rect": (0, 352, 1080, 563),
        "selector_id": "telegram.chat-row.p0001.s0000",
        "page_number": 1,
        "literal_slot": 0,
        "rect": (0, 352, 1080, 563),
    }
    message_root = ET.fromstring(message_page_xml())
    returned_root = object()
    observed = iter((
        ("observation-message", message_root, None),
        ("observation-list", returned_root, None),
        ("observation-message-revisit", message_root, None),
        ("observation-list-revisit", returned_root, None),
        ("observation-message-ambiguous-1", message_root, None),
        ("observation-list-ambiguous-1", returned_root, None),
        ("observation-message-ambiguous-2", message_root, None),
        ("observation-list-ambiguous-2", returned_root, None),
    ))
    metadata = {
        "container_type": "direct",
        "type_status": "explicit",
        "identity_status": "identified",
        "fingerprint_inputs": ["type_label", "username"],
        "fingerprint": "b" * 64,
        "linkage": {
            "entry_action_id": "action-info-entry",
            "info_observation_id": "observation-info",
            "return_action_id": "action-info-back",
            "return_observation_id": "observation-message-after-info",
        },
    }
    history = {
        "status": "complete",
        "reason_code": None,
        "latest_boundary": {"established": True},
        "earliest_boundary": {"established": True},
        "device_temporal_anchor": "2026-07-28T15:00:00+09:00",
        "limitations": [],
        "logical_message_count": 1,
        "message_occurrence_count": 1,
        "attachment_count": 0,
        "attachment_materialize_attempts": 0,
        "attachment_materialize_actions": 0,
        "attachment_display_fallbacks": 0,
        "attachment_deferred_count": 0,
    }
    written = {}

    class Device:
        def __init__(self):
            self.boundary_actions = []

        def matching_elements(self, root, element_id):
            return MATCHER.matching_elements(root, element_id)

        def click_bounds(
            self, action_id, bounds, before_observation_id, *,
            anchor="lower_third",
        ):
            self.boundary_actions.append(
                (action_id, tuple(bounds), before_observation_id, anchor)
            )

        @staticmethod
        def click(action_id, selector, before_observation_id):
            return None

        @staticmethod
        def element_selector(element_id):
            return {"selector_id": element_id}

        @staticmethod
        def canonical_sha256(value):
            return TelegramDeviceAdapter.canonical_sha256(value)

        @staticmethod
        def read_observation(observation_id, kind):
            assert observation_id == "observation-info"
            return {
                "screen_image": b"chatroom-png",
                "ui_tree": b"<chatroom/>",
            }[kind]

    monkeypatch.setattr(
        collector, "_observe", lambda device, state: next(observed)
    )
    monkeypatch.setattr(
        collector,
        "_inspect_container_metadata",
        lambda device, state, observation_id, root: (
            metadata, "observation-message-after-info", root, None
        ),
    )
    history_call = {}
    history_calls = []

    def fake_history(*args, **kwargs):
        history_calls.append(kwargs)
        history_call.update(kwargs)
        return history, "observation-history", message_root, None

    monkeypatch.setattr(
        collector, "_acquire_message_history", fake_history
    )

    def fake_write(*args, **kwargs):
        written.update(kwargs)
        return {"comparison_ref": "message-history-000001"}, None

    monkeypatch.setattr(collector, "_write_message_history", fake_write)
    device = Device()
    state = {"actions": 0, "occurrences": 0, "containers": 0}

    outputs = Outputs()
    record, _, _, reason = collector._acquire_scrollable_occurrence(
        device,
        state,
        occurrence,
        "observation-list-source",
        attachment_kinds=(),
        outputs=outputs,
        target_ref="target-ref",
        return_checkpoint=lambda device, root: object(),
        surface="default",
    )

    assert reason is None
    assert device.boundary_actions == [(
        "action-click-000001",
        occurrence["visible_rect"],
        "observation-list-source",
        "lower_third",
    )]
    assert record["display_name"] == "Synthetic Chat"
    assert record["selector_id"] == occurrence["selector_id"]
    assert record["container_ref"] == "container-000001"
    assert record["logical_chatroom_id"].startswith("telegram-chat-")
    assert record["identity_status"] == "identified"
    assert written["logical_chatroom_id"] == record["logical_chatroom_id"]
    assert history_call["logical_chatroom_id"] == (
        record["logical_chatroom_id"]
    )
    assert outputs.scopes == [(
        record["logical_chatroom_id"],
        "Synthetic Chat",
    )]

    assert outputs.records[2]["artifact_id"] == "chatroom"
    assert outputs.records[2]["value"]["source"] == {
        "action_id": "action-info-entry",
        "observation_id": "observation-info",
        "screen_artifact_id": "artifact-000001",
        "screen_path": "artifacts/chatroom-screen",
        "ui_tree_artifact_id": "artifact-000002",
        "ui_tree_path": "artifacts/chatroom-tree",
    }
    assert outputs.records[2]["value"]["display_name"] == "Synthetic Chat"
    assert outputs.records[2]["value"]["surface"] == "default"
    assert [
        item["artifact_id"] for item in outputs.records[:3]
    ] == ["chatroom-screen", "chatroom-tree", "chatroom"]

    revisit, _, _, reason = collector._acquire_scrollable_occurrence(
        device,
        state,
        occurrence,
        "observation-list-source-revisit",
        attachment_kinds=(),
        outputs=outputs,
        target_ref="target-ref",
        return_checkpoint=lambda device, root: object(),
    )

    assert reason is None
    assert revisit == {
        "revisit": True,
        "logical_chatroom_id": record["logical_chatroom_id"],
        "display_name": "Synthetic Chat",
    }
    assert len(history_calls) == 1
    assert outputs.scopes == [(
        record["logical_chatroom_id"],
        "Synthetic Chat",
    )]

    metadata["identity_status"] = "ambiguous"
    first_ambiguous, _, _, reason = collector._acquire_scrollable_occurrence(
        device,
        state,
        occurrence,
        "observation-list-source-ambiguous-1",
        attachment_kinds=(),
        outputs=outputs,
        target_ref="target-ref",
        return_checkpoint=lambda device, root: object(),
    )
    second_ambiguous, _, _, reason2 = (
        collector._acquire_scrollable_occurrence(
            device,
            state,
            occurrence,
            "observation-list-source-ambiguous-2",
            attachment_kinds=(),
            outputs=outputs,
            target_ref="target-ref",
            return_checkpoint=lambda device, root: object(),
        )
    )

    assert reason is None
    assert reason2 is None
    assert first_ambiguous["revisit"] is True
    assert second_ambiguous["revisit"] is True
    assert len(history_calls) == 1
    assert "container_revisit_ambiguity" not in state


def test_collect_activates_all_attachment_kinds(monkeypatch):
    outputs = Outputs()

    def fake_enumerate(
        device, target_ref, outputs=None, attachment_kinds=("file",)
    ):
        assert outputs is expected_outputs
        assert attachment_kinds == ("photo", "video", "file")
        return {
            "status": "complete",
            "reason_code": None,
            "target_ref": target_ref,
            "top_boundary": {
                "established": True,
                "observation_id": "observation-top",
            },
            "bottom_boundary": {
                "established": True,
                "observation_id": "observation-bottom",
                "stationary_action_ids": ("action-scroll-000002",),
                "stationary_observation_ids": ("observation-bottom",),
            },
            "archive": {
                "status": "out_of_scope",
                "reason_code": "archived_chatrooms_not_collected",
                "occurrence_count": 0,
            },
            "containers": ({
                "surface": "default",
                "outcome": "complete",
                "history": {"status": "complete"},
            },),
            "deferred": (),
            "limitations": (),
            "scroll_probes": 4,
        }

    expected_outputs = outputs
    monkeypatch.setattr(collector, "_enumerate_containers", fake_enumerate)

    report = collector.collect(object(), outputs, "account-pseudonym")

    assert report["record_kind"] == "telegram_message_history_acquisition"
    assert report["occurrence_count"] == 1
    assert len(outputs.records) == 1
    assert outputs.records[0]["artifact_id"] == "chatroom-list"
    assert outputs.records[0]["value"] == report


def test_message_parser_keeps_direct_group_channel_and_attachment_shapes():
    direct, _ = collector._parse_message_row(
        "hello\nReceived at 8:53 PM\n", "direct", None
    )
    group, sender = collector._parse_message_row(
        "Alice\nhello\nSent at 8:53 PM, Seen\n", "group", None
    )
    channel, _ = collector._parse_message_row(
        "Video, 10 seconds\nSent at 9:14 PM, Not seen\n"
        "Viewed 1 time\n",
        "channel",
        None,
    )

    assert direct["rendered"]["text"] == "hello"
    assert group["rendered"]["sender"] == sender == "Alice"
    assert channel["row_class"] == "attachment"
    assert channel["rendered"]["kind"] == "video"


@pytest.mark.parametrize(
    ("raw", "container_type", "kind"),
    [
        ("Photo\nSent at 1:44 PM, Seen\n", "group", "photo"),
        (
            "Photo\n사진입니다\nReceived at 9:13 PM\n",
            "direct",
            "photo",
        ),
        (
            "Video, 11 seconds\nSent at 1:44 PM, Not seen\n",
            "group",
            "video",
        ),
        (
            "Video, 11 seconds, 12.8 MB\n"
            "Sent at 1:44 PM, Not seen\n",
            "group",
            "video",
        ),
        (
            "Video: Downloaded 0 KB of 4.3 MB, 11 seconds\n"
            "Sent at 1:44 PM, Seen\n",
            "group",
            "video",
        ),
        (
            "Video\nDownloaded 0 KB of 2.6 MB, 12 seconds\n"
            "Sent at 1:44 PM, Seen\n",
            "direct",
            "video",
        ),
        (
            ", PDF filepaper.pdf, 421.8 KB\n"
            "Sent at 1:44 PM, Not seen\n"
            "Viewed 1 time\n",
            "channel",
            "file",
        ),
        (
            ", PDF filepaper.pdf\n"
            "Downloaded 0 KB of 421.8 KB, 421.8 KB\n"
            "Sent at 1:44 PM, Not seen\n"
            "Viewed 1 time\n",
            "channel",
            "file",
        ),
    ],
)
def test_message_parser_accepts_live_media_shapes(
    raw, container_type, kind
):
    parsed, _ = collector._parse_message_row(raw, container_type, None)

    assert parsed["row_class"] == "attachment"
    assert parsed["rendered"]["kind"] == kind


def test_message_parser_accepts_not_seen_text_status():
    parsed, _ = collector._parse_message_row(
        "[MSG-001] hello\nSent at 8:50 PM, Not seen\n",
        "direct",
        None,
    )

    assert parsed["row_class"] == "message"
    assert parsed["rendered"]["text"] == "[MSG-001] hello"
    assert parsed["rendered"]["status"] == "not_seen"


@pytest.mark.parametrize(
    ("capture_id", "suffix", "bounds"),
    [
        ("old-source135", "\n", [0, 679, 1080, 1213]),
        ("new-source135", "Reactions: \n\n", [0, 679, 1080, 1213]),
        ("new-source138", "Reactions: \n\n", [0, 1234, 1080, 1768]),
    ],
)
def test_live_video_xml_empty_reactions_keeps_exact_actionable_row(
    capture_id, suffix, bounds
):
    captures = ET.parse(ROOT / "tests/fixtures/telegram-video-reactions.xml")
    node = captures.find(f"./capture[@id='{capture_id}']/node")
    raw = "Video, 1 minute 58 seconds\n동영상 봐주세요\nReceived at 9:14 PM" + suffix
    assert node.get("text") == raw
    root = ET.fromstring(message_page_xml())
    page = collector._message_page(MATCHER, root)
    page["list"].remove(page["list"][0])
    page["list"].append(node)
    state = {"message_occurrences": 0, "messages": 0}

    rows, deferred, reason = collector._message_window(
        page, "direct", action_id="action-source",
        observation_id=capture_id, window_index=0, state=state,
    )

    assert reason is None and not deferred
    row, = rows
    assert row["row_class"] == "attachment"
    assert row["rendered"] == {"kind": "video"}
    assert row["time_label"] == "9:14 PM"
    assert row["raw"] == raw
    assert row["source"]["bounds"] == bounds
    assert row["source"]["viewport_visibility"] == "full"
    assert row["limitations"] == ["attachment_deferred_t03a"]
    assert collector._attachment_action_selector(page, row)["value"] == raw
    history = collector._finalize_message_history(
        state, rows, deferred, [], latest={"established": True},
        earliest={"established": True}, representative_action_id="action-source",
        representative_observation_id=capture_id,
    )
    assert history["status"] == "complete"
    assert history["attachment_count"] == 1
    # Classification makes a candidate, not a claim of an acquired original.
    assert history["attachment_materialize_attempts"] == 0
    assert history["attachment_deferred_count"] == 1


@pytest.mark.parametrize(
    "suffix",
    ["Reactions: 👍 1\n\n", "Reactions: \n👍 1\n", "Reactions: unknown\n\n",
     "Reactions: \n\nextra", "Reactions: \n"],
)
def test_message_parser_keeps_unreviewed_reaction_suffixes_unclassified(suffix):
    raw = "Video, 1 minute 58 seconds\n동영상 봐주세요\nReceived at 9:14 PM" + suffix
    parsed, _ = collector._parse_message_row(raw, "direct", None)

    assert parsed["row_class"] == "raw"
    assert parsed["limitations"] == ["message_shape_unclassified"]


def test_message_parser_keeps_reactions_words_in_message_body():
    raw = "Reactions: 👍 1\nReceived at 9:14 PM\n"
    parsed, _ = collector._parse_message_row(raw, "direct", None)

    assert parsed["row_class"] == "message"
    assert parsed["rendered"]["text"] == "Reactions: 👍 1"


@pytest.mark.parametrize(
    ("raw", "container_type", "row_class"),
    [
        ("hello\nSent at 13:44, Not seen\n", "direct", "message"),
        ("Photo\nSent at 13:44, Not seen\n", "direct", "attachment"),
        (
            "hello\nSent at 20:57, Not seen\nViewed 10 times\n",
            "channel",
            "message",
        ),
        (
            "Photo\nSent at 20:57, Not seen\nViewed 10 times\n",
            "channel",
            "attachment",
        ),
    ],
)
def test_message_parser_accepts_24_hour_time(
    raw, container_type, row_class
):
    parsed, _ = collector._parse_message_row(raw, container_type, None)

    assert parsed["row_class"] == row_class


def test_temporal_context_resolves_device_local_dates_for_all_messages():
    def row(row_class, raw, time_label=None):
        return {
            "row_class": row_class,
            "raw": raw,
            "time_label": time_label,
            "date_marker_label": None,
            "temporal_precision": "minute" if time_label else None,
            "temporal_resolution": "unresolved",
            "resolved_datetime": None,
        }

    yesterday = row("message", "old", "8:10 AM")
    today = row("message", "new", "8:53 PM")
    twenty_four_hour = row("message", "24-hour", "20:57")
    no_year = row("attachment", "Photo", "9:00 AM")
    explicit_year = row("message", "archive", "11:59 PM")
    rows = [
        row("date_context", "Yesterday"),
        yesterday,
        row("date_context", "Today"),
        today,
        twenty_four_hour,
        row("date_context", "July 1"),
        no_year,
        row("date_context", "December 31, 2025"),
        explicit_year,
    ]

    collector._enrich_temporal_context(
        rows, "2026-07-28T15:00:00+09:00"
    )

    assert yesterday["resolved_datetime"] == (
        "2026-07-27T08:10:00+09:00"
    )
    assert today["resolved_datetime"] == "2026-07-28T20:53:00+09:00"
    assert twenty_four_hour["resolved_datetime"] == (
        "2026-07-28T20:57:00+09:00"
    )
    assert no_year["resolved_datetime"] == "2026-07-01T09:00:00+09:00"
    assert no_year["date_marker_label"] == "July 1"
    assert explicit_year["resolved_datetime"] == (
        "2025-12-31T23:59:00+09:00"
    )
    assert {
        yesterday["temporal_resolution"],
        today["temporal_resolution"],
        twenty_four_hour["temporal_resolution"],
        no_year["temporal_resolution"],
        explicit_year["temporal_resolution"],
    } == {"exact"}


def test_system_context_appears_in_conversation_with_observed_date():
    rows = [
        {
            "row_class": "date_context",
            "raw": "November 11, 2024",
            "time_label": None,
            "date_marker_label": None,
            "temporal_precision": None,
            "temporal_resolution": "unresolved",
            "resolved_datetime": None,
        },
        {
            "row_class": "system_context",
            "raw": "Channel created",
            "rendered": {},
            "time_label": None,
            "date_marker_label": None,
            "temporal_precision": None,
            "temporal_resolution": "unresolved",
            "resolved_datetime": None,
        },
    ]

    collector._enrich_temporal_context(
        rows, "2026-07-31T15:00:00+09:00"
    )

    assert rows[1]["date_marker_label"] == "November 11, 2024"
    assert collector._conversation_text(rows) == (
        b"[November 11, 2024] system: Channel created\n"
    )


def test_message_window_assigns_occurrence_refs_bottom_to_top():
    root = ET.fromstring(message_page_xml())
    message_list = next(
        node for node in root.iter()
        if node.get("class") == "androidx.recyclerview.widget.RecyclerView"
    )
    for child in list(message_list):
        message_list.remove(child)
    for index, raw in enumerate((
        "oldest\nReceived at 8:51 PM\n",
        "middle\nReceived at 8:52 PM\n",
        "newest\nReceived at 8:53 PM\n",
    )):
        ET.SubElement(message_list, "node", {
            "package": "org.telegram.messenger",
            "class": "android.view.ViewGroup",
            "text": raw,
            "focusable": "true",
            "clickable": "false",
            "bounds": f"[0,{500 + index * 300}][1080,{800 + index * 300}]",
            "visible-to-user": "true",
        })
    page = collector._message_page(MATCHER, root)

    rows, _, reason = collector._message_window(
        page,
        "direct",
        action_id="action-1",
        observation_id="observation-1",
        window_index=0,
        state={"message_occurrences": 0},
    )

    assert reason is None
    assert [row["rendered"]["text"] for row in rows] == [
        "oldest", "middle", "newest",
    ]
    assert [row["message_occurrence_ref"] for row in rows] == [
        "message-occurrence-000003",
        "message-occurrence-000002",
        "message-occurrence-000001",
    ]


def test_message_window_keeps_clipped_text_and_defers_attachment():
    root = ET.fromstring(message_page_xml())
    message_list = next(
        node for node in root.iter()
        if node.get("class") == "androidx.recyclerview.widget.RecyclerView"
    )
    for child in list(message_list):
        message_list.remove(child)
    for raw, bounds in (
        ("top\nReceived at 8:51 PM\n", "[0,100][1080,500]"),
        ("middle\nReceived at 8:52 PM\n", "[0,500][1080,800]"),
        ("Photo\nReceived at 8:53 PM\n", "[0,1800][1080,2200]"),
    ):
        ET.SubElement(message_list, "node", {
            "package": "org.telegram.messenger",
            "class": "android.view.ViewGroup",
            "text": raw,
            "focusable": "true",
            "clickable": "false",
            "bounds": bounds,
            "visible-to-user": "true",
        })
    page = collector._message_page(MATCHER, root)

    rows, deferred, reason = collector._message_window(
        page,
        "direct",
        action_id="action-1",
        observation_id="observation-1",
        window_index=0,
        state={"message_occurrences": 0},
    )

    assert reason is None
    assert [row["rendered"]["text"] for row in rows] == ["top", "middle"]
    assert [row["source"]["viewport_visibility"] for row in rows] == [
        "clipped", "full",
    ]
    assert rows[0]["limitations"] == ["row_clipped_by_viewport"]
    assert rows[1]["limitations"] == []
    assert [(item["raw"].splitlines()[0], item["edge"]) for item in deferred] == [
        ("Photo", "bottom"),
    ]


def test_message_window_admits_actionable_bottom_clipped_file():
    raw = (
        ", PDF filepaper.pdf\n"
        "Downloaded 0 KB of 421.8 KB, 421.8 KB\n"
        "Sent at 1:44 PM, Not seen\n"
        "Viewed 1 time\n"
    )
    root = ET.fromstring(message_page_xml())
    message_list = next(
        node for node in root.iter()
        if node.get("class") == "androidx.recyclerview.widget.RecyclerView"
    )
    node = list(message_list)[0]
    node.set("text", raw)
    node.set("bounds", "[0,1800][1080,2100]")
    page = collector._message_page(MATCHER, root)

    rows, deferred, reason = collector._message_window(
        page,
        "channel",
        action_id="action-1",
        observation_id="observation-1",
        window_index=0,
        state={"message_occurrences": 0},
    )

    assert reason is None
    assert deferred == []
    assert rows[0]["row_class"] == "attachment"
    assert rows[0]["rendered"]["kind"] == "file"
    assert rows[0]["source"]["viewport_visibility"] == "clipped"
    assert collector._attachment_action_selector(page, rows[0]) is not None


def test_registers_message_refs_bottom_to_top():
    rows = [
        {"row_class": "message"},
        {"row_class": "attachment"},
        {"row_class": "message"},
    ]

    collector._register_acquired_rows({"messages": 0}, rows)

    assert [row["message_ref"] for row in rows] == [
        "message-000003",
        "message-000002",
        "message-000001",
    ]


def test_finalize_registers_unresolved_message_refs_bottom_to_top():
    rows = [
        {
            "row_class": "message",
            "raw": "older",
            "time_label": "8:51 PM",
            "date_marker_label": None,
            "temporal_precision": "minute",
            "temporal_resolution": "unresolved",
            "resolved_datetime": None,
            "limitations": [],
            "sightings": [],
        },
        {
            "row_class": "message",
            "raw": "newer",
            "time_label": "8:52 PM",
            "date_marker_label": None,
            "temporal_precision": "minute",
            "temporal_resolution": "unresolved",
            "resolved_datetime": None,
            "limitations": [],
            "sightings": [],
        },
    ]

    history = collector._finalize_message_history(
        {"messages": 0},
        rows,
        [],
        [],
        latest={"established": True},
        earliest={"established": True},
        representative_action_id="action",
        representative_observation_id="observation",
    )

    assert [row["message_ref"] for row in history["rows"]] == [
        "message-000002", "message-000001",
    ]


def message_window_row(signature, sighting):
    return {
        "signature": signature,
        "sightings": [sighting],
    }


def test_message_windows_merge_only_the_unique_overlap():
    older = [
        message_window_row(("a",), {"observation_id": "older-a"}),
        message_window_row(("b",), {"observation_id": "older-b"}),
    ]
    newer = [
        message_window_row(("b",), {"observation_id": "newer-b"}),
        message_window_row(("c",), {"observation_id": "newer-c"}),
    ]

    merged, limitations, reason = collector._merge_older_window(
        older, newer
    )

    assert reason is None
    assert limitations == []
    assert [row["signature"] for row in merged] == [
        ("a",), ("b",), ("c",)
    ]
    assert merged[1]["sightings"] == [
        {"observation_id": "older-b"},
        {"observation_id": "newer-b"},
    ]


@pytest.mark.parametrize(
    "older",
    [
        [],
        [message_window_row(("a",), {"observation_id": "older-a"})],
    ],
)
def test_message_windows_use_bottom_clipped_row_as_continuity_anchor(older):
    newer = [
        {
            **message_window_row(
                ("b",), {"observation_id": "newer-b"}
            ),
            "raw": "shared boundary row",
        },
        {
            **message_window_row(
                ("c",), {"observation_id": "newer-c"}
            ),
            "raw": "newer row",
        },
    ]
    clipped = [{
        "reason": "row_clipped_by_viewport",
        "edge": "bottom",
        "raw": "shared boundary row",
        "source": {"bounds": [0, 1800, 1080, 2200]},
    }]

    merged, limitations, reason = collector._merge_older_window(
        older, newer, clipped
    )

    assert reason is None
    assert limitations == []
    assert merged == [*older, *newer]


@pytest.mark.parametrize("artifact_class,output_kind", [("account", "ui_record"), ("attachment", "ui_record"), ("message", "audit_record")])
def test_message_singleton_rejects_wrong_class_or_kind_before_retention(tmp_path, artifact_class, output_kind):
    def run(context):
        action = context.journal.record_action("source", status="success")
        outputs = adapter.TelegramOutputs(context, SimpleNamespace(actions={"source": action}), "target").for_chatroom("telegram-chat-" + "a" * 64, "Chat")
        outputs.begin_history()
        with pytest.raises(adapter.TelegramAdapterError, match="real history scope"):
            outputs.write_json(
                artifact_id="message-ui-000001", artifact_class=artifact_class, output_kind=output_kind,
                value={"record_kind": "message", "message_ref": "message-000001", "logical_chatroom_id": outputs.logical_chatroom_id},
                action_id="source", observation_id="snapshot", comparison_ref="message-history-000001",
            )
        assert context.artifacts.records == ()
        assert len(context.attempts) == 1
        assert not any(item.item_type == "message" for item in context.items)
        context.interrupt_open_attempts("invalid_message_output")
        return Outcome(OutcomeStatus.PARTIAL, "invalid_message_output")

    AcquisitionRuntime(tmp_path, FakeDevice()).run(PROFILE, SYSTEM_UI, Route.MATERIALIZE, {}, run, run_id="invalid-message")


def test_partial_history_uses_real_scope_before_acquisition_and_retains_message(tmp_path, monkeypatch):
    chat_id = "telegram-chat-" + "a" * 64

    def run(context):
        output_device = SimpleNamespace(actions={}, observation_actions={}, read_observation=lambda observation_id, kind: b"screen" if kind == "screen_image" else b"<hierarchy/>")
        outputs = adapter.TelegramOutputs(context, output_device, "target").for_chatroom(chat_id, "Chat")

        def boundary(*args):
            # Called by the real collector before reading/normalizing history.
            assert outputs.history_attempt_id is not None
            scope = next(item for item in context.attempts if item.attempt_id == outputs.history_attempt_id)
            assert scope.started_at is not None and scope.ended_at is None
            output_device.actions["read-history"] = context.journal.record_action("read_history", status="success")
            output_device.observation_actions["source-history"] = output_device.actions["read-history"]
            return {"established": False, "last_action_id": "read-history", "last_observation_id": "source-history"}, "source-history", object(), "latest_boundary_not_established"

        monkeypatch.setattr(collector, "_normalize_history_boundary", boundary)
        monkeypatch.setattr(collector, "_message_page", lambda *args: object())
        monkeypatch.setattr(collector, "_message_window", lambda *args, **kwargs: ([{
            "row_class": "message", "raw": "retained before interruption", "rendered": {"text": "retained before interruption"},
            "sightings": [{"action_id": "read-history", "observation_id": "source-history", "bounds": (1, 2, 3, 4)}],
            "time_label": None, "date_marker_label": None, "temporal_precision": None, "temporal_resolution": "unresolved", "resolved_datetime": None, "limitations": [],
        }], [], None))
        history, _, _, reason = collector._acquire_message_history(object(), {"messages": 0}, "initial", object(), "direct", outputs=outputs, attachment_kinds=())
        assert reason is None and history["status"] == "partial"
        capture, reason = collector._write_message_history(output_device, outputs, target_ref="target", logical_chatroom_id=chat_id,
            container_ref="container-000001", occurrence_ref="occurrence-000001", metadata={}, history=history)
        assert reason is None and capture["message_artifact_ids"] == ["message-ui-000001"]
        context.interrupt_open_attempts("latest_boundary_not_established")
        return Outcome(OutcomeStatus.PARTIAL, "latest_boundary_not_established")

    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(PROFILE, SYSTEM_UI, Route.MATERIALIZE, {}, run, run_id="partial-history")
    message = next(item for item in result.items if item.item_type == "message")
    artifact = next(item for item in result.artifacts if item.artifact_id == message.source["artifact_id"])
    original = json.loads((result.run_dir / artifact.relative_path).read_text())
    assert original["raw"] == "retained before interruption"
    scope = next(item for item in result.attempts if item.attempt_id == message.source["collection_attempt_id"])
    assert artifact.attempt_id == scope.attempt_id
    assert scope.procedure_status.value == "interrupted" and scope.acquisition_status.value == "partial"
    events = [json.loads(line) for line in (result.run_dir / "events.jsonl").read_text().splitlines()]
    assert all(scope.started_at <= event["timestamp"] <= scope.ended_at for event in events if event["event_type"] in {"action", "artifact_retained"})


def test_message_history_retries_window_with_only_clipped_rows(monkeypatch):
    row = lambda label: {
        "row_class": "message",
        "raw": label,
        "signature": (label,),
        "sightings": [],
    }
    newer = row("newer")
    windows = iter((
        ([newer], [], None),
        ([], [{"edge": "bottom", "raw": "tall-clipped"}], None),
        ([row("older"), row("newer")], [], None),
    ))
    scrolls = iter((
        ("action-scroll-1", True, None),
        ("action-scroll-2", True, None),
        ("action-scroll-3", False, None),
        ("action-scroll-4", False, None),
    ))
    observations = iter((
        "observation-clipped",
        "observation-older",
        "observation-stationary-1",
        "observation-stationary-2",
    ))
    root = object()
    monkeypatch.setattr(
        collector,
        "_normalize_history_boundary",
        lambda *args: (
            {
                "established": True,
                "stationary_action_ids": ["action-latest"],
                "stationary_observation_ids": ["observation-latest"],
            },
            "observation-latest",
            root,
            None,
        ),
    )
    monkeypatch.setattr(collector, "_message_page", lambda *args: object())
    monkeypatch.setattr(
        collector, "_message_window", lambda *args, **kwargs: next(windows)
    )
    monkeypatch.setattr(
        collector, "_scroll", lambda *args, **kwargs: next(scrolls)
    )
    monkeypatch.setattr(
        collector,
        "_observe",
        lambda *args: (next(observations), root, None),
    )
    monkeypatch.setattr(
        collector,
        "_dispatch_attachments",
        lambda *args, **kwargs: (args[5], args[3], None),
    )
    monkeypatch.setattr(
        collector,
        "_finalize_message_history",
        lambda state, rows, deferred, limitations, **kwargs: {
            "rows": rows,
            **kwargs,
        },
    )

    history, _, _, reason = collector._acquire_message_history(
        object(),
        {"stagnation_rounds": 2, "messages": 0},
        "observation-initial",
        root,
        "direct",
        outputs=None,
    )

    assert reason is None
    assert history["earliest"]["established"] is True
    assert [item["raw"] for item in history["rows"]] == ["older", "newer"]


def test_message_windows_preserve_missing_and_ambiguous_overlap():
    row = lambda label, source: message_window_row(
        (label,), {"observation_id": source}
    )

    missing = collector._merge_older_window(
        [row("a", "older-a")],
        [row("b", "newer-b")],
    )
    ambiguous = collector._merge_older_window(
        [row("a", "older-a1"), row("a", "older-a2")],
        [
            row("a", "newer-a1"),
            row("a", "newer-a2"),
            row("c", "newer-c"),
        ],
    )

    assert missing[0] is None
    assert missing[2] == "message_window_continuity_not_established"
    assert ambiguous[2] is None
    assert ambiguous[1] == ["message_revisit_ambiguity"]
    assert len(ambiguous[0]) == 5


def test_scroll_probe_count_does_not_define_a_boundary():
    calls = []

    class Device:
        @staticmethod
        def scroll(action_id, observation_id, *, direction):
            calls.append((action_id, observation_id, direction))
            return True

    state = {"actions": 0, "scrolls": 256}

    action_id, moved, reason = collector._scroll(
        Device(),
        state,
        "observation-000001",
        "backward",
    )

    assert (action_id, moved, reason) == (
        "action-scroll-000001",
        True,
        None,
    )
    assert calls == [(
        "action-scroll-000001",
        "observation-000001",
        "backward",
    )]
    assert state["scrolls"] == 257


def test_latest_boundary_retries_go_to_bottom_once_after_scroll_progress():
    class Device:
        def __init__(self):
            self.observation_xml = iter((
                message_page_xml(go_to_bottom=True),
                message_page_xml(go_to_bottom=True),
                message_page_xml(go_to_bottom=True),
                message_page_xml(go_to_bottom=True),
                message_page_xml(go_to_bottom=True),
                message_page_xml(go_to_bottom=True),
            ))
            self.observations = {}
            self.scroll_results = iter((True, True, False, False))
            self.clicks = []

        def matching_elements(self, root, element_id):
            return MATCHER.matching_elements(root, element_id)

        @staticmethod
        def element_selector(element_id):
            assert element_id == "telegram.message.go-to-bottom"
            return {
                "selector_id":
                    "telegram.message.go-to-bottom.content-description",
            }

        def click(self, action_id, selector, before_observation_id):
            self.clicks.append((
                action_id,
                selector["selector_id"],
                before_observation_id,
            ))

        def scroll(self, action_id, observation_id, *, direction):
            assert direction == "forward"
            return next(self.scroll_results)

        def observe(self, observation_id):
            self.observations[observation_id] = next(
                self.observation_xml
            ).encode()
            return {"observation_id": observation_id}

        def read_observation(self, observation_id, kind):
            assert kind == "ui_tree"
            return self.observations[observation_id]

    device = Device()
    state = {
        "actions": 0,
        "observations": 0,
        "scrolls": 0,
        "stagnation_rounds": 2,
    }

    latest, _, _, reason = collector._normalize_history_boundary(
        device,
        state,
        "observation-initial",
        ET.fromstring(message_page_xml(go_to_bottom=True)),
        "forward",
    )

    assert reason is None
    assert latest["established"] is True
    assert [item[0] for item in device.clicks] == [
        "action-go-to-bottom-000001",
        "action-go-to-bottom-000003",
    ]
    assert latest["go_to_bottom_action_ids"] == [
        "action-go-to-bottom-000001",
        "action-go-to-bottom-000003",
    ]
    assert latest["go_to_bottom_succeeded"] is False
    assert latest["scroll_action_count"] == 4
    assert state["scrolls"] == 4


def test_attachment_row_selector_contains_no_coordinate_fields():
    selector = collector._semantic_node_selector(
        ET.fromstring(
            '<node package="org.telegram.messenger" '
            'class="android.view.ViewGroup" text="Photo&#10;8:53 PM"/>'
        ),
        "telegram.attachment-row",
    )

    encoded = json.dumps(selector)
    assert selector["kind"] == "text"
    assert "bounds" not in encoded
    assert "coordinate" not in encoded


class CollectorDevice:
    def __init__(self, after, *, pulled=b"original", final=None):
        self.stagnation_rounds = 2
        self.inventory_probes = 2
        self.after = after
        self.final = after if final is None else final
        self.pulled = pulled
        self.unbased_snapshots = 0
        self.observation_xml = iter(
            (menu_xml(), message_page_xml())
        )
        self.observations = {}
        self.semantic_actions = []
        self.boundary_actions = []

    def matching_elements(self, root, element_id):
        return MATCHER.matching_elements(root, element_id)

    def element_selector(self, element_id):
        entry = PROFILE.selectors[element_id]
        return {
            **entry["selector"],
            "constraints": entry["constraints"],
        }

    def click(self, action_id, selector, before_observation_id):
        self.semantic_actions.append(selector["selector_id"])

    def click_bounds(
        self,
        action_id,
        bounds,
        before_observation_id,
        *,
        anchor="lower_third",
    ):
        self.boundary_actions.append((
            action_id,
            tuple(bounds),
            before_observation_id,
            anchor,
        ))

    def observe(self, observation_id):
        self.observations[observation_id] = next(
            self.observation_xml
        ).encode()
        return {"observation_id": observation_id}

    def read_observation(self, observation_id, kind):
        if kind == "screen_image":
            return (
                b"menu-png"
                if observation_id == "observation-000001"
                else b"source-png"
            )
        return self.observations[observation_id]

    def snapshot_external_files(
        self, action_id, observation_id, roots, baseline=None
    ):
        if baseline is not None:
            return self.after
        self.unbased_snapshots += 1
        return () if self.unbased_snapshots == 1 else self.final

    def pull_external_file(
        self, action_id, remote_path, observation_id, roots
    ):
        return self.pulled

    @staticmethod
    def canonical_sha256(value):
        return "f" * 64


class FileCollectorDevice(CollectorDevice):
    def __init__(self, after, raw):
        super().__init__(after, pulled=b"x" * after[0][1])
        self.observation_xml = iter((
            menu_xml("Save to Downloads"),
            message_page_xml(raw),
        ))
        self.boundary_actions = []

    def click_bounds(
        self,
        action_id,
        bounds,
        before_observation_id,
        *,
        anchor="lower_third",
    ):
        self.boundary_actions.append((
            action_id,
            tuple(bounds),
            before_observation_id,
            anchor,
        ))

    def snapshot_downloads(
        self, action_id, observation_id, baseline=None
    ):
        if baseline is not None:
            return self.after
        self.unbased_snapshots += 1
        return () if self.unbased_snapshots == 1 else self.final

    def pull_download(
        self, action_id, remote_path, observation_id
    ):
        return self.pulled


class Outputs:
    def __init__(self):
        self.records = []
        self.scopes = []

    def write_json(self, **kwargs):
        self.records.append(kwargs)
        return SimpleNamespace(
            artifact_id=f"artifact-{len(self.records):06d}",
            relative_path=f"artifacts/{kwargs['artifact_id']}",
        )

    def write_bytes(self, **kwargs):
        self.records.append(kwargs)
        return SimpleNamespace(
            artifact_id=f"artifact-{len(self.records):06d}",
            relative_path=f"artifacts/{kwargs['artifact_id']}",
        )

    def for_chatroom(
        self,
        logical_chatroom_id,
        display_name,
        **_scope,
    ):
        self.scopes.append((logical_chatroom_id, display_name))
        return self


def test_empty_attachment_kinds_record_rows_without_materialize(monkeypatch):
    calls = []
    monkeypatch.setattr(
        collector,
        "_materialize_attachment",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    root = object()
    row = {
        "row_class": "attachment",
        "rendered": {"kind": "photo"},
    }

    observation_id, returned_root, reason = collector._dispatch_attachments(
        object(),
        {},
        [row],
        root,
        object(),
        "observation-000001",
        "direct",
        attachment_kinds=(),
        outputs=Outputs(),
        target_ref="target-ref",
        logical_chatroom_id="telegram-chat-" + "a" * 64,
        container_ref="container-000001",
        occurrence_ref="occurrence-000001",
    )

    assert (observation_id, returned_root, reason) == (
        "observation-000001",
        root,
        None,
    )
    assert calls == []
    assert "attachment_outcome" not in row


def test_dispatch_attempts_file_in_direct_container(monkeypatch):
    calls = []
    returned_root = object()
    row = {
        "row_class": "attachment",
        "rendered": {"kind": "file"},
        "limitations": ["attachment_deferred_t03a"],
    }

    def materialize(*args, **kwargs):
        calls.append(kwargs)
        return (
            "observation-after",
            returned_root,
            {
                "attachment_ref": "attachment-000001",
                "status": "materialized",
                "limitation": None,
            },
            None,
        )

    monkeypatch.setattr(
        collector, "_materialize_attachment", materialize
    )
    observation_id, root, reason = collector._dispatch_attachments(
        object(),
        {"messages": 0},
        [row],
        object(),
        object(),
        "observation-before",
        "direct",
        attachment_kinds=("file",),
        outputs=Outputs(),
        target_ref="target-ref",
        logical_chatroom_id="telegram-chat-" + "a" * 64,
        container_ref="container-000001",
        occurrence_ref="occurrence-000001",
    )

    assert (observation_id, root, reason) == (
        "observation-after",
        returned_root,
        None,
    )
    assert len(calls) == 1
    assert calls[0]["logical_chatroom_id"] == (
        "telegram-chat-" + "a" * 64
    )
    assert calls[0]["message_ref"] == "message-000001"
    assert row["message_ref"] == "message-000001"
    assert row["attachment_outcome"] == "materialized"


def test_dispatch_attempts_attachments_bottom_to_top(monkeypatch):
    calls = []
    root = object()
    rows = [
        {
            "row_class": "attachment",
            "raw": "upper",
            "rendered": {"kind": "photo"},
            "limitations": [],
        },
        {
            "row_class": "attachment",
            "raw": "lower",
            "rendered": {"kind": "photo"},
            "limitations": [],
        },
    ]

    def materialize(*args, **kwargs):
        calls.append(args[2]["raw"])
        return (
            "observation-after",
            root,
            {
                "attachment_ref": f"attachment-{len(calls):06d}",
                "status": "materialized",
                "limitation": None,
            },
            None,
        )

    monkeypatch.setattr(
        collector, "_materialize_attachment", materialize
    )

    _, _, reason = collector._dispatch_attachments(
        object(),
        {"messages": 0},
        rows,
        root,
        object(),
        "observation-before",
        "direct",
        attachment_kinds=("photo",),
        outputs=Outputs(),
        target_ref="target-ref",
        logical_chatroom_id="telegram-chat-" + "a" * 64,
        container_ref="container-000001",
        occurrence_ref="occurrence-000001",
    )

    assert reason is None
    assert calls == ["lower", "upper"]
    assert [row["message_ref"] for row in rows] == [
        "message-000002", "message-000001",
    ]


def test_message_history_outputs_include_logical_chatroom_id():
    class ScreenDevice:
        @staticmethod
        def read_observation(observation_id, kind):
            return {
                ("observation-latest", "screen_image"): b"latest-png",
                ("observation-latest", "ui_tree"): b"<latest/>",
                ("observation-earliest", "screen_image"): b"earliest-png",
                ("observation-earliest", "ui_tree"): b"<earliest/>",
                ("observation-message", "screen_image"): b"message-png",
                ("observation-message", "ui_tree"): b"<message/>",
            }[(observation_id, kind)]

    history = {
        "representative_action_id": "action-history",
        "representative_observation_id": "observation-history",
        "latest_boundary": {
            "established": True,
            "stationary_action_ids": ["action-latest"],
            "stationary_observation_ids": ["observation-latest"],
        },
        "earliest_boundary": {
            "established": True,
            "stationary_action_ids": ["action-earliest"],
            "stationary_observation_ids": ["observation-earliest"],
        },
        "device_temporal_anchor": "2026-07-28T15:00:00+09:00",
        "limitations": [],
        "deferred": [],
        "rows": [
            {
                "row_class": "message",
                "message_ref": "message-000001",
                "sightings": [{
                    "action_id": "action-message",
                    "observation_id": "observation-message",
                    "bounds": (20, 500, 900, 650),
                }],
                "raw": "hello",
                "rendered": {"text": "hello", "sender": "Alice"},
                "time_label": "8:53 PM",
                "date_marker_label": "July 28, 2026",
                "temporal_precision": "minute",
                "temporal_resolution": "exact",
                "resolved_datetime": "2026-07-28T20:53:00",
                "limitations": [],
            },
            {
                "row_class": "message",
                "message_ref": "message-000002",
                "sightings": [{
                    "action_id": "action-message",
                    "observation_id": "observation-message",
                    "bounds": (20, 660, 900, 810),
                }],
                "raw": "later",
                "rendered": {"text": "later", "direction": "sent"},
                "time_label": "8:54 PM",
                "date_marker_label": "July 28, 2026",
                "temporal_precision": "minute",
                "temporal_resolution": "exact",
                "resolved_datetime": "2026-07-28T20:54:00",
                "limitations": [],
            },
        ],
    }
    outputs = Outputs()

    capture, reason = collector._write_message_history(
        ScreenDevice(),
        outputs,
        target_ref="target-ref",
        logical_chatroom_id="telegram-chat-" + "a" * 64,
        container_ref="container-000001",
        occurrence_ref="occurrence-000001",
        metadata={"container_type": "direct"},
        history=history,
    )

    assert reason is None
    assert capture["message_artifact_ids"] == [
        "message-ui-000001",
        "message-ui-000002",
    ]
    assert [
        item["artifact_id"] for item in outputs.records
    ] == [
        "container-history-context-000001",
        "message-window-screen-000001",
        "message-window-tree-000001",
        "message-ui-000001",
        "message-ui-000002",
        "conversation-000001",
        "message-history-latest-screen-000001",
        "message-history-latest-tree-000001",
        "message-history-earliest-screen-000001",
        "message-history-earliest-tree-000001",
    ]
    message = next(
        item["value"]
        for item in outputs.records
        if item["artifact_id"] == "message-ui-000001"
    )
    assert message["source_evidence"] == {
        "message_window_ref": "message-window-000001",
        "screen_artifact_id": "artifact-000002",
        "screen_path": "artifacts/message-window-screen-000001",
        "ui_tree_artifact_id": "artifact-000003",
        "ui_tree_path": "artifacts/message-window-tree-000001",
        "source_snapshot_id": "telegram-snapshot:observation-message",
        "bounds": [20, 500, 900, 650],
    }
    json_values = [
        item["value"] for item in outputs.records
        if "value" in item and isinstance(item["value"], dict)
    ]
    assert len(json_values) == 3
    assert {
        value["logical_chatroom_id"] for value in json_values
    } == {"telegram-chat-" + "a" * 64}
    context = next(
        value for value in json_values
        if value["record_kind"] == "container_history_context"
    )
    assert context["device_temporal_anchor"] == (
        "2026-07-28T15:00:00+09:00"
    )
    conversation = next(
        item["value"].decode("utf-8")
        for item in outputs.records
        if item["artifact_id"] == "conversation-000001"
    )
    assert conversation.index("Alice: hello") < conversation.index(
        "sent: later"
    )


def run_photo_materialize(
    after, *, pulled=b"original", final=None, device_factory=CollectorDevice
):
    root = ET.fromstring(message_page_xml())
    page = collector._message_page(MATCHER, root)
    rows, deferred, reason = collector._message_window(
        page,
        "group",
        action_id="action-source",
        observation_id="observation-source",
        window_index=0,
        state={"message_occurrences": 0},
    )
    assert not deferred and reason is None
    device = device_factory(after, pulled=pulled, final=final)
    outputs = Outputs()
    result = collector._materialize_attachment(
        device,
        {
            "actions": 0,
            "observations": 0,
            "attachments": 0,
        },
        rows[0],
        root,
        page,
        "observation-source",
        rows,
        container_type="group",
        outputs=outputs,
        target_ref="target",
        logical_chatroom_id="telegram-chat-" + "a" * 64,
        container_ref="container",
        occurrence_ref="occurrence",
        message_ref="message-000001",
    )
    return result, device, outputs


def test_photo_materialize_opens_row_boundary_menu():
    after = (("/sdcard/Pictures/Telegram/a.jpg", 8, 11),)

    result, device, outputs = run_photo_materialize(after)

    assert result[3] is None
    assert device.boundary_actions == [
        (
            "action-attachment-open-000002",
            (1070, 500, 1080, 800),
            "observation-source",
            "center",
        ),
        (
            "action-save-to-gallery-000003",
            (500, 800, 1080, 1040),
            "observation-000001",
            "center",
        ),
    ]
    assert device.semantic_actions == []
    assert len(outputs.records) == 5
    json_values = [
        record["value"]
        for record in outputs.records
        if isinstance(record.get("value"), dict)
    ]
    assert {
        (value["logical_chatroom_id"], value["message_ref"])
        for value in json_values
    } == {("telegram-chat-" + "a" * 64, "message-000001")}
    attachment = next(
        value for value in json_values
        if value["record_kind"] == "attachment"
    )
    assert attachment["selected_device_basename"] == "a.jpg"
    assert attachment["retained_basename"] == "a.jpg"
    screen = next(
        record
        for record in outputs.records
        if record["output_kind"] == "screen_image"
    )
    assert screen["artifact_id"] == "attachment-menu-screen-000001"
    assert screen["action_id"] == "action-attachment-open-000002"
    assert screen["observation_id"] == "observation-000001"
    assert screen["value"] == b"menu-png"
    tree = next(
        record
        for record in outputs.records
        if record["output_kind"] == "ui_hierarchy"
    )
    assert tree["artifact_id"] == "attachment-menu-tree-000001"
    assert tree["action_id"] == "action-attachment-open-000002"
    assert tree["observation_id"] == "observation-000001"
    assert tree["value"].startswith(b"<hierarchy")


def test_photo_without_save_action_records_fallback_and_restores_window():
    class SaveActionUnavailableDevice(CollectorDevice):
        def __init__(self, after, *, pulled=b"original", final=None):
            super().__init__(after, pulled=pulled, final=final)
            self.observation_xml = iter((
                menu_xml("Reply"),
                message_page_xml(),
            ))
            self.back_actions = []

        def back(self, action_id, before_observation_id):
            self.back_actions.append((action_id, before_observation_id))

        def snapshot_external_files(
            self, action_id, observation_id, roots, baseline=None
        ):
            if baseline is not None:
                raise RuntimeError("inventory did not change")
            return super().snapshot_external_files(
                action_id, observation_id, roots, baseline
            )

    result, device, outputs = run_photo_materialize(
        (), device_factory=SaveActionUnavailableDevice
    )

    assert result[3] is None
    assert result[2] == {
        "attachment_ref": "attachment-000001",
        "status": "display_fallback",
        "limitation":
            "attachment_materialize_save_action_unavailable",
    }
    assert device.back_actions == [(
        "action-attachment-menu-dismiss-000003",
        "observation-000001",
    )]
    attachment = next(
        record["value"]
        for record in outputs.records
        if record["output_kind"] == "ui_record"
    )
    assert attachment["outcome"] == "display_fallback"
    assert (
        attachment["limitation"]
        == "attachment_materialize_save_action_unavailable"
    )
    assert {
        record["output_kind"] for record in outputs.records
    } == {"ui_record", "screen_image", "ui_hierarchy", "audit_record"}
    audit = next(record["value"] for record in outputs.records if record["output_kind"] == "audit_record")
    assert audit["after_inventory_status"] == "not_performed"
    assert audit["after_count"] is None
    assert audit["after_manifest_sha256"] is None


@pytest.mark.parametrize("title", ["Telegram", "Waiting for network", "Waiting for network...", "Connecting..."])
@pytest.mark.parametrize("account_return", [False, True])
@pytest.mark.parametrize("version", ["12.9.0", "12.9.2"])
def test_enumeration_establishes_top_before_first_rows(monkeypatch, title, account_return, version):
    matcher = TelegramDeviceAdapter(SimpleNamespace(app_profile=ProfileStore(ROOT / "profiles").load_app("telegram", version)))
    title_bounds = "[82,110][772,201]" if account_return else "[66,106][756,197]"
    bottom = default_list_xml(title=title, rows=()).replace("[66,106][756,197]", title_bounds).encode()
    top = default_list_xml(title=title, rows=({"text": "First chat", "bounds": "[0,400][1080,600]"},)).replace("[66,106][756,197]", title_bounds).encode()

    class Device:
        stagnation_rounds = 1
        current = bottom
        observations = {}

        def matching_elements(self, root, element_id):
            return matcher.matching_elements(root, element_id)

        def click_bounds(self, action_id, bounds, observation_id, *, anchor):
            assert tuple(bounds) == ((82, 110, 772, 201) if account_return else (66, 106, 756, 197))
            self.current = top

        def observe(self, observation_id):
            self.observations[observation_id] = self.current
            return {"observation_id": observation_id}

        def read_observation(self, observation_id, kind):
            return self.observations[observation_id]

    if account_return:
        def account(device, state, observation_id, root, outputs, target_ref):
            device.current = bottom
            return None, observation_id, root, collector._default_list(device, root), None
        monkeypatch.setattr(collector, "_acquire_account_profile", account)
    seen = []
    def enumerate_rows(page, page_number):
        seen.extend(n.get("text") for n in page["list"].iter() if n.get("text") == "First chat")
        return None, "test_stop_after_first_enumeration"
    monkeypatch.setattr(collector, "_safe_occurrences", enumerate_rows)
    report = collector._enumerate_containers(Device(), "test-account", outputs=object() if account_return else None)
    assert seen == ["First chat"]
    assert report["top_boundary"]["established"] is True


def test_stalled_loading_dialog_records_fallback_and_restores_window():
    class LoadingDevice(CollectorDevice):
        def __init__(self, after, *, pulled=b"original", final=None):
            super().__init__(after, pulled=pulled, final=final)
            self.observation_xml = iter((
                menu_xml(), loading_dialog_xml(), loading_dialog_xml(),
                message_page_xml(),
            ))
            self.back_actions = []

        def back(self, action_id, before_observation_id):
            self.back_actions.append((action_id, before_observation_id))

        def read_observation(self, observation_id, kind):
            if kind == "screen_image":
                return {
                    "observation-000001": b"menu-png",
                    "observation-000002": b"loading-png",
                }.get(observation_id, b"source-png")
            return self.observations[observation_id]

    result, device, outputs = run_photo_materialize(
        (), device_factory=LoadingDevice
    )

    assert result[3] is None
    assert result[2] == {
        "attachment_ref": "attachment-000001",
        "status": "display_fallback",
        "limitation": "attachment_materialize_download_unavailable",
    }
    assert device.back_actions == [(
        "action-attachment-loading-dismiss-000005",
        "observation-000003",
    )]
    record = next(
        item["value"] for item in outputs.records
        if item["output_kind"] == "ui_record"
    )
    assert record["limitation"] == (
        "attachment_materialize_download_unavailable"
    )
    loading_screen = next(
        item for item in outputs.records
        if item["artifact_id"] == "attachment-unavailable-screen-000001"
    )
    assert loading_screen["value"] == b"loading-png"


def test_auto_dismissed_loading_dialog_does_not_back_out_of_chat():
    class AutoDismissDevice(CollectorDevice):
        def __init__(self, after, *, pulled=b"original", final=None):
            super().__init__(after, pulled=pulled, final=final)
            self.observation_xml = iter((
                menu_xml(), loading_dialog_xml(), message_page_xml(),
            ))
            self.back_actions = []

        def back(self, action_id, before_observation_id):
            self.back_actions.append((action_id, before_observation_id))

    after = (("/sdcard/Pictures/Telegram/a.jpg", 8, 11),)
    result, device, _ = run_photo_materialize(
        after,
        device_factory=AutoDismissDevice,
    )

    assert result[3] is None
    assert result[2]["status"] == "materialized"
    assert device.back_actions == []


def test_failed_loading_inventory_records_fallback_without_output_failure():
    class FailedInventoryDevice(CollectorDevice):
        def __init__(self, after, *, pulled=b"original", final=None):
            super().__init__(after, pulled=pulled, final=final)
            self.observation_xml = iter((
                menu_xml(), loading_dialog_xml(), loading_dialog_xml(),
                message_page_xml(),
            ))

        def snapshot_external_files(
            self, action_id, observation_id, roots, baseline=None
        ):
            if baseline is not None:
                raise RuntimeError("inventory did not change")
            return super().snapshot_external_files(
                action_id, observation_id, roots, baseline
            )

        def back(self, action_id, before_observation_id):
            pass

    class ActionAwareOutputs(Outputs):
        def write_json(self, **kwargs):
            assert kwargs["action_id"] != "action-snapshot-gallery-000004"
            return super().write_json(**kwargs)

    root = ET.fromstring(message_page_xml())
    page = collector._message_page(MATCHER, root)
    rows, deferred, reason = collector._message_window(
        page,
        "group",
        action_id="action-source",
        observation_id="observation-source",
        window_index=0,
        state={"message_occurrences": 0},
    )
    assert not deferred and reason is None
    device = FailedInventoryDevice(())
    outputs = ActionAwareOutputs()

    result = collector._materialize_attachment(
        device,
        {"actions": 0, "observations": 0, "attachments": 0},
        rows[0],
        root,
        page,
        "observation-source",
        rows,
        container_type="group",
        outputs=outputs,
        target_ref="target",
        logical_chatroom_id="telegram-chat-" + "a" * 64,
        container_ref="container",
        occurrence_ref="occurrence",
        message_ref="message-000001",
    )

    assert result[3] is None
    assert result[2]["limitation"] == (
        "attachment_materialize_download_unavailable"
    )


def test_loading_dialog_waits_for_inventory_change_before_fallback():
    class ProgressLoadingDevice(CollectorDevice):
        def __init__(self, after, *, pulled=b"original", final=None):
            super().__init__(after, pulled=pulled, final=final)
            self.observation_xml = iter((
                menu_xml(), loading_dialog_xml(), loading_dialog_xml(),
                message_page_xml(),
            ))
            self.back_actions = []

        def back(self, action_id, before_observation_id):
            self.back_actions.append((action_id, before_observation_id))

    after = (("/sdcard/Pictures/Telegram/a.jpg", 8, 11),)
    result, device, outputs = run_photo_materialize(
        after,
        pulled=b"original",
        device_factory=ProgressLoadingDevice,
    )

    assert result[3] is None
    assert result[2]["status"] == "materialized"
    assert device.back_actions == [(
        "action-attachment-loading-dismiss-000005",
        "observation-000003",
    )]
    assert any(
        item["output_kind"] == "original_artifact"
        for item in outputs.records
    )


def test_preinventory_failure_does_not_claim_materialize_menu_screen():
    class PreinventoryFailureDevice(CollectorDevice):
        def snapshot_external_files(self, *args, **kwargs):
            raise RuntimeError

    result, _, outputs = run_photo_materialize(
        (), device_factory=PreinventoryFailureDevice
    )

    assert result[3] is None
    assert not any(
        record["output_kind"] == "screen_image"
        for record in outputs.records
    )
    ui_record = next(
        record["value"]
        for record in outputs.records
        if record["output_kind"] == "ui_record"
    )
    assert (
        ui_record["limitation"]
        == "attachment_materialize_preinventory_failed"
    )


def test_file_materialize_opens_row_boundary_menu():
    raw = (
        ", TXT file240420.txt, 8.2 KB&#10;"
        "Sent at 8:55 PM, Seen&#10;"
    )
    root = ET.fromstring(message_page_xml(raw))
    page = collector._message_page(MATCHER, root)
    rows, deferred, reason = collector._message_window(
        page,
        "group",
        action_id="action-source",
        observation_id="observation-source",
        window_index=0,
        state={"message_occurrences": 0},
    )
    assert not deferred and reason is None
    device = FileCollectorDevice(
        (("/sdcard/Download/240420.txt", 8_192, 11),),
        raw,
    )

    result = collector._materialize_attachment(
        device,
        {
            "actions": 0,
            "observations": 0,
            "attachments": 0,
        },
        rows[0],
        root,
        page,
        "observation-source",
        rows,
        container_type="group",
        outputs=Outputs(),
        target_ref="target",
        logical_chatroom_id="telegram-chat-" + "a" * 64,
        container_ref="container",
        occurrence_ref="occurrence",
        message_ref="message-000001",
    )

    assert result[3] is None
    assert device.boundary_actions == [
        (
            "action-attachment-open-000002",
            (1070, 500, 1080, 800),
            "observation-source",
            "center",
        ),
        (
            "action-save-to-downloads-000003",
            (500, 800, 1080, 1040),
            "observation-000001",
            "center",
        ),
    ]
    assert device.semantic_actions == []


@pytest.mark.parametrize(
    ("after", "limitation"),
    [
        ((), "attachment_materialize_no_candidate"),
        (
            (
                ("/sdcard/Pictures/Telegram/a.jpg", 1, 11),
                ("/sdcard/Pictures/Telegram/b.jpg", 2, 11),
            ),
            "attachment_materialize_ambiguous_candidates",
        ),
    ],
)
def test_photo_materialize_preserves_candidate_failures(after, limitation):
    result, _, outputs = run_photo_materialize(after)

    assert result[3] is None
    ui_record = next(
        record["value"]
        for record in outputs.records
        if record["output_kind"] == "ui_record"
    )
    assert ui_record["outcome"] == "display_fallback"
    assert ui_record["limitation"] == limitation


def test_photo_materialize_rejects_pulled_size_mismatch():
    after = (("/sdcard/Pictures/Telegram/a.jpg", 8, 11),)

    result, _, outputs = run_photo_materialize(
        after, pulled=b"short"
    )

    assert result[3] is None
    ui_record = next(
        record["value"]
        for record in outputs.records
        if record["output_kind"] == "ui_record"
    )
    assert ui_record["outcome"] == "display_fallback"
    assert ui_record["limitation"] == (
        "attachment_materialize_size_mismatch"
    )
    assert not any(
        record["output_kind"] == "original_artifact"
        for record in outputs.records
    )


def test_photo_materialize_rejects_changed_postpull_inventory():
    after = (("/sdcard/Pictures/Telegram/a.jpg", 8, 11),)
    final = (("/sdcard/Pictures/Telegram/a.jpg", 9, 12),)

    result, _, outputs = run_photo_materialize(after, final=final)

    assert result[3] is None
    ui_record = next(
        record["value"]
        for record in outputs.records
        if record["output_kind"] == "ui_record"
    )
    assert ui_record["outcome"] == "display_fallback"
    assert ui_record["limitation"] == (
        "attachment_materialize_postpull_changed"
    )
    assert not any(
        record["output_kind"] == "original_artifact"
        for record in outputs.records
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("NTS_eTaxInvoice (3).html", "NTS_eTaxInvoice (3).html"),
        (" unsafe/한글\\name\x00.txt ", "unsafe_한글_name_.txt"),
    ],
)
def test_normalizes_only_unsafe_retained_basename_characters(
    value, expected
):
    assert adapter.normalize_retained_basename(value) == expected


def test_entrypoint_stops_when_telegram_app_is_not_ready(monkeypatch):
    import aura.apps.telegram as telegram

    dispatched = []
    runtime_device = SimpleNamespace(last_action=None)
    context = SimpleNamespace(
        base_context={
            "condition": {
                "target": {
                    "kind": "account",
                    "ref": "account-pseudonym",
                }
            }
        },
        app_profile=PROFILE,
        device=SimpleNamespace(
            app_start=lambda package: None,
            last_action=None,
        ),
        start_app=lambda: False,
    )
    monkeypatch.setattr(
        telegram,
        "TelegramDeviceAdapter",
        lambda runtime_context: runtime_device,
    )
    monkeypatch.setattr(
        telegram,
        "TelegramOutputs",
        lambda runtime_context, device, target_ref: object(),
    )
    monkeypatch.setattr(
        collector,
        "collect",
        lambda *args: dispatched.append("collect")
        or {"status": "complete", "reason_code": None},
    )

    outcome = telegram.collect(context)

    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.reason == "telegram_app_start_not_verified"
    assert dispatched == []


def test_current_runtime_groups_telegram_outputs_by_chatroom(
    monkeypatch, tmp_path
):
    def fake_collect(device, outputs, target_ref):
        device.scroll("action-scroll-000001", "observation-000001")
        device.observe("observation-000001")
        for artifact_id, output_kind, value, suffix in (
            ("account", "ui_record", b'{"record_kind":"account"}', ".json"),
            ("account-screen", "screen_image", b"account", ".png"),
            ("account-tree", "ui_hierarchy", b"<account/>", ".xml"),
        ):
            outputs.write_bytes(
                artifact_id=artifact_id,
                output_kind=output_kind,
                artifact_class="account",
                value=value,
                action_id="action-scroll-000001",
                observation_id="observation-000001",
                comparison_ref="account-profile",
                suffix=suffix,
            )
        outputs.write_json(
            artifact_id="chatroom-list",
            output_kind="ui_record",
            artifact_class="chatroom_list",
            value={"record_kind": "chatroom_list"},
            action_id="action-scroll-000001",
            observation_id="observation-000001",
            comparison_ref="chatroom-list",
        )
        chatroom_id = "telegram-chat-" + "a" * 64
        chatroom = outputs.for_chatroom(
            chatroom_id, "Example / 사용자\\테스트"
        )
        chatroom.begin_history()
        device.scroll("action-history-acquisition", "observation-000001")
        common = {
            "action_id": "action-scroll-000001",
            "observation_id": "observation-000001",
        }
        chatroom.write_json(
            artifact_id="chatroom",
            output_kind="ui_record",
            artifact_class="chatroom",
            value={
                "record_kind": "chatroom",
                "logical_chatroom_id": chatroom_id,
                "display_name": "Example / 사용자\\테스트",
            },
            comparison_ref=chatroom_id,
            **common,
        )
        chatroom.write_bytes(
            artifact_id="chatroom-screen",
            output_kind="screen_image",
            artifact_class="chatroom",
            value=b"chatroom",
            comparison_ref=chatroom_id,
            suffix=".png",
            **common,
        )
        chatroom.write_bytes(
            artifact_id="chatroom-tree",
            output_kind="ui_hierarchy",
            artifact_class="chatroom",
            value=b"<chatroom/>",
            comparison_ref=chatroom_id,
            suffix=".xml",
            **common,
        )
        chatroom.write_json(
            artifact_id="container-history-context-000001",
            output_kind="ui_record",
            artifact_class="message",
            value={"record_kind": "container_history_context"},
            comparison_ref="message-history-000001",
            **common,
        )
        for boundary in ("latest", "earliest"):
            chatroom.write_bytes(
                artifact_id=(
                    f"message-history-{boundary}-screen-000001"
                ),
                output_kind="screen_image",
                artifact_class="message",
                value=boundary.encode(),
                comparison_ref="message-history-000001",
                suffix=".png",
                **common,
            )
            chatroom.write_bytes(
                artifact_id=f"message-history-{boundary}-tree-000001",
                output_kind="ui_hierarchy",
                artifact_class="message",
                value=f"<{boundary}/>".encode(),
                comparison_ref="message-history-000001",
                suffix=".xml",
                **common,
            )
        chatroom.write_json(
            artifact_id="message-ui-000001",
            output_kind="ui_record",
            artifact_class="message",
            value={"record_kind": "message", "message_ref": "message-000001", "logical_chatroom_id": chatroom_id},
            comparison_ref="message-history-000001",
            **common,
        )
        chatroom.write_bytes(
            artifact_id="message-screen-000001",
            output_kind="screen_image",
            artifact_class="message",
            value=b"message",
            comparison_ref="message-history-000001",
            suffix=".png",
            **common,
        )
        chatroom.write_bytes(
            artifact_id="message-tree-000001",
            output_kind="ui_hierarchy",
            artifact_class="message",
            value=b"<message/>",
            comparison_ref="message-history-000001",
            suffix=".xml",
            **common,
        )
        chatroom.write_bytes(
            artifact_id="attachment-original-000001",
            output_kind="original_artifact",
            artifact_class="attachment",
            value=b"artifact",
            comparison_ref="attachment-000001",
            suffix=".html",
            retained_basename="NTS_eTaxInvoice (3).html",
            **common,
        )
        for artifact_id, output_kind, suffix in (
            ("attachment-ui-000001", "ui_record", ".json"),
                (
                    "attachment-menu-screen-000001",
                    "screen_image",
                    ".png",
                ),
                (
                    "attachment-menu-tree-000001",
                    "ui_hierarchy",
                    ".xml",
                ),
                ("attachment-audit-000001", "audit_record", ".json"),
        ):
            chatroom.write_bytes(
                artifact_id=artifact_id,
                output_kind=output_kind,
                artifact_class="attachment",
                value=b"artifact",
                comparison_ref="attachment-000001",
                suffix=suffix,
                **common,
            )
        outputs.write_json(
            artifact_id="account-closure-000001",
            output_kind="audit_record",
            artifact_class="message",
            value={"record_kind": "account_closure"},
            action_id="action-scroll-000001",
            observation_id="observation-000001",
            comparison_ref="account-closure-000001",
        )
        return {"status": "complete", "reason_code": None}

    monkeypatch.setattr(collector, "collect", fake_collect)
    result = AcquisitionRuntime(
        tmp_path,
        FakeDevice(
            hierarchy_before=(
                '<hierarchy><node package="org.telegram.messenger"/>'
                "</hierarchy>"
            )
        ),
    ).run(
        PROFILE,
        SYSTEM_UI,
        Route.MATERIALIZE,
        {"target": {"kind": "account", "ref": "account-pseudonym"}},
        collect,
        run_id="telegram-linked",
    )
    manifest = json.loads(
        (result.run_dir / "acquisition.json").read_text(encoding="utf-8")
    )

    assert result.outcome.status is OutcomeStatus.COMPLETE
    chatroom_root = (
        "artifacts/telegram/chatrooms/"
        + "telegram-chat-"
        + "a" * 64
        + " (Example _ 사용자_테스트)"
    )
    source_artifacts = {a for o in result.observations for a in (o.screen_artifact_id, o.hierarchy_artifact_id)}
    assert [item.relative_path for item in result.artifacts if item.artifact_id not in source_artifacts] == [
        "artifacts/telegram/account/account.json",
        "artifacts/telegram/account/account.png",
        "artifacts/telegram/account/account.xml",
        "artifacts/telegram/account/chatroom-list.json",
        f"{chatroom_root}/chatroom.json",
        f"{chatroom_root}/chatroom.png",
        f"{chatroom_root}/chatroom.xml",
        f"{chatroom_root}/history.json",
        f"{chatroom_root}/observations/latest-boundary.png",
        f"{chatroom_root}/observations/latest-boundary.xml",
        f"{chatroom_root}/observations/earliest-boundary.png",
        f"{chatroom_root}/observations/earliest-boundary.xml",
        f"{chatroom_root}/messages/message-000001.json",
        f"{chatroom_root}/messages/message-000001.png",
        f"{chatroom_root}/messages/message-000001.xml",
        (
            f"{chatroom_root}/attachments/attachment-000001/"
            "NTS_eTaxInvoice (3).html"
        ),
        (
            f"{chatroom_root}/attachments/attachment-000001/"
            "record.json"
        ),
        (
            f"{chatroom_root}/attachments/attachment-000001/"
            "materialize-menu-screen.png"
        ),
        (
            f"{chatroom_root}/attachments/attachment-000001/"
            "materialize-menu-screen.xml"
        ),
        (
            f"{chatroom_root}/attachments/attachment-000001/"
            "audit.json"
        ),
        "artifacts/telegram/account/account-closure.json",
    ]
    assert not any(
        item.relative_path.startswith("artifacts/observations/")
        for item in result.artifacts
    )
    chatroom_artifact = next(
        item
        for item in result.artifacts
        if item.relative_path == f"{chatroom_root}/chatroom.json"
    )
    chatroom_record = json.loads(
        (
            result.run_dir
            / chatroom_artifact.relative_path
        ).read_text(encoding="utf-8")
    )
    assert chatroom_record["display_name"] == "Example / 사용자\\테스트"
    encoded = (
        result.run_dir / chatroom_artifact.relative_path
    ).read_bytes()
    assert b'\n  "display_name":' in encoded
    assert encoded.endswith(b"\n")
    assert manifest["run"]["app_version"] == "12.9.2"
    assert manifest["run"]["route"] == "materialize"
    assert manifest["run"]["condition"]["target"]["kind"] == "account"
    message_item = next(item for item in result.items if item.item_type == "message")
    message_artifact = next(item for item in result.artifacts if item.relative_path.endswith("messages/message-000001.json"))
    assert message_item.source["collection_attempt_id"] == message_artifact.attempt_id
    assert message_item.source["artifact_id"] == message_artifact.artifact_id
    assert message_item.source["message_ref"] == "message-000001"
    assert not any(item.acquisition_item_id == message_item.acquisition_item_id for item in result.attempts)
    scope = next(item for item in result.attempts if item.attempt_id == message_artifact.attempt_id)
    events = [json.loads(line) for line in (result.run_dir / "events.jsonl").read_text().splitlines()]
    acquired = next(event for event in events if event.get("action") == "action-history-acquisition")
    assert scope.started_at <= acquired["timestamp"] <= scope.ended_at
    assert all(scope.started_at <= event["timestamp"] <= scope.ended_at for event in events if event["event_type"] in {"action", "artifact_retained"} and event.get("attempt_id", event.get("details", {}).get("attempt_id")) == scope.attempt_id)
    assert {item.item_type for item in result.items} >= {
        "account",
        "conversation",
        "message",
        "file",
    }
    assert {
        attempt.acquisition_status.value for attempt in result.attempts
    } == {"acquired"}


def test_telegram_output_pair_creates_formal_observation(tmp_path):
    def retain_pair(context):
        source_action = context.journal.record_action(
            "open_account", status="success"
        )
        output_device = SimpleNamespace(
            actions={"open_account": source_action},
            read_observation=lambda observation_id, kind: {
                "screen_image": b"screen",
                "ui_tree": b"<hierarchy/>",
            }[kind],
        )
        outputs = adapter.TelegramOutputs(
            context, output_device, "account-pseudonym"
        )
        evidence = outputs.write_observation_pair(
            artifact_class="account",
            screen_artifact_id="account-screen",
            tree_artifact_id="account-tree",
            action_id="open_account",
            observation_id="source-observation",
            comparison_ref="account-profile",
        )
        context.complete_open_attempts(AcquisitionStatus.ACQUIRED)
        return Outcome(
            OutcomeStatus.COMPLETE,
            details={"observation_id": evidence["observation_id"]},
        )

    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(
        PROFILE,
        SYSTEM_UI,
        Route.MATERIALIZE,
        {"target": {"kind": "account", "ref": "account-pseudonym"}},
        retain_pair,
        run_id="telegram-observation-link",
    )

    assert len(result.observations) == 1
    observation = result.observations[0]
    assert result.outcome.details["observation_id"] == (
        observation.observation_id
    )
    assert {
        artifact.observation_id
        for artifact in result.artifacts
    } == {observation.observation_id}


def assert_telegram_json_provenance(result):
    acquisition = json.loads((result.run_dir / "acquisition.json").read_text())
    indexes = {
        name: {row[key]: row for row in acquisition[name]}
        for name, key in (
            ("items", "acquisition_item_id"), ("attempts", "attempt_id"),
            ("artifacts", "artifact_id"), ("observations", "observation_id"),
        )
    }
    members = {a.relative_path: (result.run_dir / a.relative_path).read_bytes() for a in result.artifacts}
    errors = []
    runpy.run_path(str(ROOT / "tools/validate_aura_results.py"))["_validate_app_json"](
        indexes, members, acquisition["run"], errors,
    )
    assert errors == []


@pytest.mark.parametrize("scope", ["account", "chatroom", "message", "attachment", "traversal"])
def test_json_retains_every_referenced_telegram_snapshot(tmp_path, scope):
    chat_id = "telegram-chat-" + "a" * 64

    def retain(context):
        device = TelegramDeviceAdapter(context)
        for local_id in ("entry", "secondary", "return"):
            context.device.hierarchy_value = f'<hierarchy text="{local_id}"/>'
            device.observe(local_id)
        outputs = adapter.TelegramOutputs(context, device, "account-pseudonym")
        if scope in {"chatroom", "message", "attachment"}:
            outputs = outputs.for_chatroom(chat_id, "Test chat")
        if scope == "message":
            outputs.begin_history()
        action = context.journal.record_action("write", status="success")
        device.actions["write"] = action
        artifact_id, artifact_class, comparison_ref = {
            "account": ("account", "account", "account-profile"),
            "chatroom": ("chatroom", "chatroom", chat_id),
            "message": ("message-ui-000001", "message", "message-history-000001"),
            "attachment": ("attachment-ui-000001", "attachment", "attachment-000001"),
            "traversal": ("chatroom-list", "chatroom_list", "chatroom-list"),
        }[scope]
        outputs.write_json(
            artifact_id=artifact_id, artifact_class=artifact_class,
            output_kind="ui_record", action_id="write", observation_id="entry",
            comparison_ref=comparison_ref,
            value={
                "record_kind": scope, "logical_chatroom_id": chat_id,
                "message_ref": "message-000001", "rendered": {"kind": "photo"},
                "source": {"observation_id": "entry"},
                "metadata": {"return_observation_id": "return"},
                "sightings": [{"observation_id": "secondary"}],
                "boundary": {"stationary_observation_ids": ["secondary", "return"]},
            },
        )
        context.complete_open_attempts(AcquisitionStatus.ACQUIRED)
        return Outcome(OutcomeStatus.COMPLETE)

    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(
        PROFILE, SYSTEM_UI, Route.MATERIALIZE,
        {"target": {"kind": "account", "ref": "account-pseudonym"}},
        retain, run_id=f"telegram-references-{scope}",
    )
    assert {o.source_snapshot_id for o in result.observations} == {
        "telegram-snapshot:entry", "telegram-snapshot:secondary", "telegram-snapshot:return",
    }
    artifacts = {a.artifact_id: a for a in result.artifacts}
    for observation in result.observations:
        screen = artifacts[observation.screen_artifact_id]
        tree = artifacts[observation.hierarchy_artifact_id]
        assert screen.observation_id == tree.observation_id == observation.observation_id
        assert screen.attempt_id == tree.attempt_id == observation.attempt_id
        local_id = observation.source_snapshot_id.split(":")[1]
        assert (result.run_dir / tree.relative_path).read_bytes() == f'<hierarchy text="{local_id}"/>'.encode()
    events = [json.loads(line) for line in (result.run_dir / "events.jsonl").read_text().splitlines()]
    actions = {e["action_id"]: e for e in events if e.get("event_type") == "action"}
    assert all(actions[actions[o.action_id]["context"]["source_action_id"]]["action"] == "telegram_observe_initial" for o in result.observations)
    assert_telegram_json_provenance(result)


def test_history_boundaries_keep_common_observation_ids(tmp_path):
    def retain(context):
        device = TelegramDeviceAdapter(context)
        device.observe("local-boundary")
        device.actions["history"] = context.journal.record_action("history", status="success")
        outputs = adapter.TelegramOutputs(context, device, "account-pseudonym").for_chatroom(
            "telegram-chat-" + "a" * 64, "Test chat",
        )
        outputs.begin_history()
        boundary = {"established": True, "stationary_action_ids": ["history"], "stationary_observation_ids": ["local-boundary"]}
        capture, reason = collector._write_message_history(
            device, outputs, target_ref="account-pseudonym",
            logical_chatroom_id="telegram-chat-" + "a" * 64,
            container_ref="container-000001", occurrence_ref="occurrence-000001",
            metadata={}, history={
                "representative_action_id": "history", "representative_observation_id": "local-boundary",
                "latest_boundary": boundary, "earliest_boundary": boundary,
                "limitations": [], "deferred": [], "rows": [],
            },
        )
        assert reason is None
        adapter.TelegramOutputs(context, device, "account-pseudonym").write_json(
            artifact_id="chatroom-list", artifact_class="chatroom_list", output_kind="ui_record",
            action_id="history", observation_id="local-boundary", comparison_ref="chatroom-list",
            value={"containers": [{"logical_chatroom_id": outputs.logical_chatroom_id, "capture": capture}]},
        )
        context.complete_open_attempts(AcquisitionStatus.ACQUIRED)
        return Outcome(OutcomeStatus.COMPLETE, details=capture)

    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(
        PROFILE, SYSTEM_UI, Route.MATERIALIZE,
        {"target": {"kind": "account", "ref": "account-pseudonym"}},
        retain, run_id="telegram-boundary-reference",
    )
    artifacts = {a.artifact_id: a for a in result.artifacts}
    for evidence in result.outcome.details["boundary_artifact_ids"].values():
        assert evidence["observation_id"] == artifacts[evidence["screen_artifact_id"]].observation_id
        assert evidence["observation_id"] == artifacts[evidence["ui_tree_artifact_id"]].observation_id
    assert_telegram_json_provenance(result)


def test_single_message_keeps_common_observation_id(tmp_path):
    hierarchy = message_page_xml("hello&#10;Received at 8:53 PM&#10;").replace('scrollable="true"', 'scrollable="false"')

    def retain(context):
        device = TelegramDeviceAdapter(context)
        device.observe("local-message")
        device.actions["read"] = context.journal.record_action("read", status="success")
        outputs = adapter.TelegramOutputs(context, device, "account-pseudonym").for_chatroom(
            "telegram-chat-" + "a" * 64, "Test chat",
        )
        outputs.begin_history()
        capture, reason = collector._write_single_text_message(
            device, outputs, target_ref="account-pseudonym",
            logical_chatroom_id="telegram-chat-" + "a" * 64,
            container_ref="container-000001", occurrence_ref="occurrence-000001",
            action_id="read", observation_id="local-message", root=ET.fromstring(hierarchy),
        )
        assert reason is None
        adapter.TelegramOutputs(context, device, "account-pseudonym").write_json(
            artifact_id="chatroom-list", artifact_class="chatroom_list", output_kind="ui_record",
            action_id="read", observation_id="local-message", comparison_ref="chatroom-list",
            value={"containers": [{"logical_chatroom_id": outputs.logical_chatroom_id, "message_capture": capture}]},
        )
        context.complete_open_attempts(AcquisitionStatus.ACQUIRED)
        return Outcome(OutcomeStatus.COMPLETE)

    result = AcquisitionRuntime(tmp_path, FakeDevice(hierarchy_before=hierarchy)).run(
        PROFILE, SYSTEM_UI, Route.MATERIALIZE,
        {"target": {"kind": "account", "ref": "account-pseudonym"}},
        retain, run_id="telegram-single-reference",
    )
    message = next(a for a in result.artifacts if a.kind == "ui_record")
    evidence = json.loads((result.run_dir / message.relative_path).read_text())["source_evidence"]
    assert evidence["observation_id"] == result.observations[0].observation_id
    assert_telegram_json_provenance(result)


def test_unvisited_telegram_boundaries_do_not_emit_null_observation_refs(tmp_path):
    def retain(context):
        device = TelegramDeviceAdapter(context)
        report = collector._enumerate_containers(device, "account-pseudonym")
        assert report["archive"]["status"] == "out_of_scope"
        assert report["top_boundary"]["established"] is False
        device.actions["report"] = context.journal.record_action("report", status="success")
        adapter.TelegramOutputs(context, device, "account-pseudonym").write_json(
            artifact_id="chatroom-list", artifact_class="chatroom_list", output_kind="ui_record",
            action_id="report", observation_id="observation-000001", comparison_ref="chatroom-list",
            value=report,
        )
        context.interrupt_open_attempts(report["reason_code"])
        return Outcome(OutcomeStatus.PARTIAL, report["reason_code"])

    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(
        PROFILE, SYSTEM_UI, Route.MATERIALIZE, {}, retain, run_id="telegram-unvisited-boundaries",
    )
    assert_telegram_json_provenance(result)


def test_original_retains_exact_source_in_its_own_attempt(tmp_path):
    def retain(context):
        device = TelegramDeviceAdapter(context)
        device.observe("after-save")
        device.actions["pull"] = context.journal.record_action("pull_file", status="success")
        outputs = adapter.TelegramOutputs(context, device, "account-pseudonym")
        outputs.write_observation_pair(
            artifact_class="account", screen_artifact_id="account-screen", tree_artifact_id="account-tree",
            action_id="pull", observation_id="after-save", comparison_ref="account-profile",
        )
        outputs.for_chatroom("telegram-chat-" + "a" * 64, "Test chat").write_bytes(
            artifact_id="attachment-original-000001", artifact_class="attachment", output_kind="original_artifact",
            action_id="pull", observation_id="after-save", comparison_ref="attachment-000001",
            value=b"original file bytes", suffix=".bin", retained_basename="sample.bin",
        )
        context.complete_open_attempts(AcquisitionStatus.ACQUIRED)
        return Outcome(OutcomeStatus.COMPLETE)

    result = AcquisitionRuntime(tmp_path, FakeDevice(hierarchy_before='<hierarchy text="after-save"/>')).run(
        PROFILE, SYSTEM_UI, Route.MATERIALIZE, {}, retain, run_id="telegram-original-source",
    )
    original = next(a for a in result.artifacts if a.kind == "original_artifact")
    assert original.observation_id is not None
    observation = next(o for o in result.observations if o.observation_id == original.observation_id)
    assert observation.source_snapshot_id == "telegram-snapshot:after-save"
    assert observation.attempt_id == original.attempt_id != result.observations[0].attempt_id
    tree = next(a for a in result.artifacts if a.artifact_id == observation.hierarchy_artifact_id)
    assert (result.run_dir / tree.relative_path).read_bytes() == b'<hierarchy text="after-save"/>'
    assert (result.run_dir / original.relative_path).read_bytes() == b"original file bytes"


@pytest.mark.parametrize(("limitation", "expected_source"), [
    ("attachment_materialize_download_unavailable", "unavailable"),
    ("attachment_materialize_save_action_unavailable", "menu"),
    ("attachment_materialize_no_candidate", "decision"),
])
def test_attachment_outcome_uses_explicit_failure_evidence(tmp_path, limitation, expected_source):
    def retain(context):
        device = TelegramDeviceAdapter(context)
        for local_id in ("message", "menu", "unavailable", "decision"):
            action = context.journal.record_action(f"open_{local_id}", status="success")
            device._remember(local_id, action)
            context.device.hierarchy_value = f'<hierarchy text="{local_id}"/>'
            device.observe(local_id)
        outputs = adapter.TelegramOutputs(context, device, "account-pseudonym").for_chatroom(
            "telegram-chat-" + "a" * 64, "Test chat",
        )
        _, reason = collector._write_attachment_outputs(
            device, outputs, attachment_ref="attachment-000001", target_ref="account-pseudonym",
            logical_chatroom_id=outputs.logical_chatroom_id, container_ref="container-000001",
            occurrence_ref="occurrence-000001", message_ref="message-000001",
            row={"source": {"action_id": "message", "observation_id": "message"}, "raw": "Photo", "rendered": {"kind": "photo"}, "time_label": None},
            outcome="display_fallback", limitation=limitation, before=(), after=None if expected_source == "menu" else (),
            candidates=(), original=None, decision_action_id="decision", decision_observation_id="decision",
            menu_action_id="menu", menu_observation_id="menu",
            unavailable_action_id="unavailable" if expected_source == "unavailable" else None,
            unavailable_observation_id="unavailable" if expected_source == "unavailable" else None,
        )
        assert reason is None
        return Outcome(OutcomeStatus.PARTIAL, limitation)

    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(
        PROFILE, SYSTEM_UI, Route.MATERIALIZE, {}, retain, run_id="telegram-outcome-source",
    )
    outcome = result.outcomes[0]
    assert outcome.observation_id is not None and outcome.action_id is not None
    observation = next(o for o in result.observations if o.observation_id == outcome.observation_id)
    assert observation.source_snapshot_id == f"telegram-snapshot:{expected_source}"
    assert observation.attempt_id == outcome.attempt_id
    events = [json.loads(line) for line in (result.run_dir / "events.jsonl").read_text().splitlines()]
    action = next(e for e in events if e.get("event_type") == "action" and e.get("action_id") == outcome.action_id)
    assert action["attempt_id"] == outcome.attempt_id
    assert action["context"]["source_snapshot_id"] == f"telegram-snapshot:{expected_source}"
    assert_telegram_json_provenance(result)


def test_photo_observation_reuses_existing_attachment_attempt(tmp_path):
    def retain_photo(context):
        source_action = context.journal.record_action(
            "open_photo", status="success"
        )
        output_device = SimpleNamespace(
            actions={"open_photo": source_action},
            observation_actions={"source-observation": source_action},
            read_observation=lambda observation_id, kind: {
                "screen_image": b"screen",
                "ui_tree": b"<hierarchy/>",
            }[kind],
        )
        outputs = adapter.TelegramOutputs(
            context, output_device, "account-pseudonym"
        ).for_chatroom(
            "telegram-chat-" + "a" * 64,
            "Test chat",
        )
        outputs.write_json(
            artifact_id="attachment-ui-000001",
            output_kind="ui_record",
            artifact_class="attachment",
            value={
                "record_kind": "attachment",
                "attachment_ref": "attachment-000001",
                "rendered": {"kind": "photo"},
                "outcome": "display_fallback",
                "limitation": (
                    "attachment_materialize_save_action_unavailable"
                ),
            },
            action_id="open_photo",
            observation_id="source-observation",
            comparison_ref="attachment-000001",
        )
        outputs.write_observation_pair(
            artifact_class="attachment",
            screen_artifact_id="attachment-unavailable-screen-000001",
            tree_artifact_id="attachment-unavailable-tree-000001",
            action_id="open_photo",
            observation_id="source-observation",
            comparison_ref="attachment-000001",
        )
        outputs.write_json(
            artifact_id="attachment-audit-000001",
            output_kind="audit_record",
            artifact_class="attachment",
            value={
                "record_kind": "attachment_materialize_audit",
                "attachment_ref": "attachment-000001",
            },
            action_id="open_photo",
            observation_id="source-observation",
            comparison_ref="attachment-000001",
        )
        assert context.attempts[0].ended_at is None
        outputs.finish_attachment("attachment-000001", "attachment_materialize_save_action_unavailable")
        return Outcome(OutcomeStatus.PARTIAL, "photo_not_materialized")

    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(
        PROFILE,
        SYSTEM_UI,
        Route.MATERIALIZE,
        {"target": {"kind": "account", "ref": "account-pseudonym"}},
        retain_photo,
        run_id="telegram-photo-observation",
    )

    assert [item.item_type for item in result.items] == ["photo"]
    assert {artifact.attempt_id for artifact in result.artifacts} == {
        result.attempts[0].attempt_id
    }


def test_display_fallback_records_attachment_action_context(
    monkeypatch, tmp_path
):
    def fake_collect(device, outputs, target_ref):
        device.scroll("action-scroll-000001", "observation-source")
        device.observe("observation-source")
        chatroom_id = "telegram-chat-" + "a" * 64
        outputs.for_chatroom(chatroom_id, "Synthetic Chat").write_json(
            artifact_id="attachment-ui-000001",
            output_kind="ui_record",
            artifact_class="attachment",
            value={
                "record_kind": "attachment",
                "outcome": "display_fallback",
                "limitation": "attachment_materialize_no_candidate",
                "attachment_ref": "attachment-000001",
                "logical_chatroom_id": chatroom_id,
                "container_ref": "container-000001",
                "occurrence_ref": "occurrence-000001",
                "message_ref": "message-000001",
                "rendered": {"kind": "photo"},
                "source": {"observation_id": "observation-source"},
            },
            action_id="action-scroll-000001",
            observation_id="observation-source",
            comparison_ref="attachment-000001",
        )
        return {
            "status": "partial",
            "reason_code": "attachment_materialize_no_candidate",
        }

    monkeypatch.setattr(collector, "collect", fake_collect)
    result = AcquisitionRuntime(
        tmp_path,
        FakeDevice(
            hierarchy_before=(
                '<hierarchy><node package="org.telegram.messenger"/>'
                "</hierarchy>"
            )
        ),
    ).run(
        PROFILE,
        SYSTEM_UI,
        Route.MATERIALIZE,
        {"target": {"kind": "account", "ref": "account-pseudonym"}},
        collect,
        run_id="telegram-partial",
    )

    incomplete = result.outcomes[0]
    assert incomplete.reason == "attachment_materialize_no_candidate"
    assert incomplete.context["target_ref"] == "account-pseudonym"
    assert incomplete.context["logical_chatroom_id"] == (
        "telegram-chat-" + "a" * 64
    )
    assert incomplete.context["container_ref"] == "container-000001"
    assert incomplete.context["occurrence_ref"] == "occurrence-000001"
    assert incomplete.context["message_ref"] == "message-000001"
    assert incomplete.context["attachment_kind"] == "photo"
    assert incomplete.context["source_snapshot_id"] == (
        "telegram-snapshot:observation-source"
    )
    assert result.attempts[0].procedure_status.value == "interrupted"
