import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest

from aura.artifacts import ArtifactStore
from aura.apps.notion import attachments, databases, pages
from aura.apps.notion.support import NotionCollectorError
from aura.journal import EventJournal
from aura.profiles import ProfileStore
from aura.runtime import RunContext
from aura.ui import UiRuntime


ROOT = Path(__file__).resolve().parents[1]
PARENT_ID = "11111111-1111-4111-8111-111111111111"
CHILD_ID = "22222222-2222-4222-8222-222222222222"
SUBCHILD_ID = "66666666-6666-8666-8666-666666666666"
DATABASE_ID = "33333333-3333-4333-8333-333333333333"
SHARED_ID = "44444444-4444-4444-8444-444444444444"
TEAM_ID = "55555555-5555-4555-8555-555555555555"


def _page_row(section, page_id, title, top, indent):
    return (
        '<node package="notion.id" class="android.view.View" '
        f'resource-id="home-tab.{section}.page-row.{page_id}" '
        f'enabled="true" bounds="[60,{top}][1020,{top + 125}]">'
        '<node package="notion.id" class="android.view.View" '
        f'enabled="true" bounds="[60,{top}][1020,{top + 125}]">'
        '<node package="notion.id" class="android.view.View" '
        f'resource-id="home-tab.{section}.page-row.expand" '
        f'enabled="true" bounds="[{indent},{top + 36}]'
        f'[{indent + 72},{top + 108}]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        f'text="{title}" enabled="true" '
        f'bounds="[{indent + 204},{top + 36}]'
        f'[{indent + 600},{top + 108}]"/>'
        "</node></node>"
    )


def home_tree():
    xml = (
        "<hierarchy>"
        '<node package="notion.id" class="android.view.View" '
        'resource-id="vision_tab_Home" selected="true" enabled="true" '
        'bounds="[204,80][460,224]"/>'
        '<node package="notion.id" class="android.view.View" '
        'resource-id="home-tab.sections.private-header" clickable="true" '
        'enabled="true" bounds="[0,809][1080,953]"/>'
        f"{_page_row('private', PARENT_ID, 'Parent', 953, 60)}"
        f"{_page_row('private', CHILD_ID, 'Child', 1078, 168)}"
        f"{_page_row('private', DATABASE_ID, 'Tasks', 1203, 60)}"
        '<node package="notion.id" class="android.view.View" '
        'resource-id="home-tab.sections.shared-header" clickable="true" '
        'enabled="true" bounds="[0,1337][1080,1481]"/>'
        f"{_page_row('shared', SHARED_ID, 'Duplicate', 1481, 60)}"
        '<node package="notion.id" class="android.view.View" '
        'resource-id="home-tab.sections.teamspaces-header" clickable="true" '
        'enabled="true" bounds="[0,1625][1080,1769]"/>'
        f"{_page_row('teamspaces', TEAM_ID, 'Team Page', 1769, 60)}"
        f"{_page_row('favorites', PARENT_ID, 'Parent', 1894, 60)}"
        "</hierarchy>"
    )
    return ET.fromstring(xml)


def general_page_tree(*, include_database_controls=False):
    controls = (
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="Filter and Sort" clickable="true" '
        'bounds="[612,1029][708,1128]"/>'
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="Edit view layout, grouping and more..." '
        'clickable="true" bounds="[708,1029][804,1128]"/>'
        if include_database_controls
        else ""
    )
    return ET.fromstring(
        "<hierarchy>"
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="Back" clickable="true" bounds="[48,99][156,210]"/>'
        '<node package="notion.id" class="android.widget.EditText" '
        'text="Parent" clickable="true" bounds="[54,492][1026,615]"/>'
        '<node package="notion.id" class="android.widget.EditText" '
        'text="Parent&#10;&#10;First paragraph&#10;Checklist item" '
        'clickable="false" bounds="[0,234][1080,2400]"/>'
        f"{controls}</hierarchy>"
    )


def block_editor_page_tree(*, include_body=True):
    body = (
        '<node package="notion.id" class="android.widget.EditText" '
        'resource-id=":r1e:" text="https://example.com" clickable="true" '
        'enabled="true" bounds="[72,543][1008,630]"/>'
        '<node package="notion.id" class="android.widget.EditText" '
        'resource-id=":r1f:" text="" clickable="true" enabled="true" '
        'bounds="[72,663][1008,750]"/>'
        if include_body
        else ""
    )
    return ET.fromstring(
        '<hierarchy>'
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="Back" clickable="true" bounds="[48,99][156,210]"/>'
        '<node package="notion.id" class="android.widget.EditText" '
        'resource-id=":r1d:" text="AURA Attachment Test" '
        'clickable="true" enabled="true" bounds="[54,234][1026,480]"/>'
        f'{body}'
        '</hierarchy>'
    )


def database_page_tree():
    return ET.fromstring(
        "<hierarchy>"
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="Back" clickable="true" bounds="[48,99][156,210]"/>'
        '<node package="notion.id" class="android.widget.EditText" '
        'text="Tasks" clickable="true" bounds="[72,450][498,573]"/>'
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="Filter and Sort" clickable="true" '
        'bounds="[612,1029][708,1128]"/>'
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="Edit view layout, grouping and more..." '
        'clickable="true" bounds="[708,1029][804,1128]"/>'
        "</hierarchy>"
    )


def block_database_item_tree():
    return ET.fromstring(
        "<hierarchy>"
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="Back" clickable="true" bounds="[48,99][156,210]"/>'
        '<node package="notion.id" class="android.widget.EditText" '
        'text="엄마한테 전화하기" clickable="true" '
        'bounds="[54,492][1026,615]"/>'
        '<node package="notion.id" class="android.view.View" '
        'text="Files &amp; media" bounds="[60,660][420,765]"/>'
        '<node package="notion.id" class="android.view.View" '
        'text="Empty" bounds="[432,660][1026,765]"/>'
        '<node package="notion.id" class="android.view.View" '
        'text="상태" bounds="[60,774][420,879]"/>'
        '<node package="notion.id" class="android.view.View" '
        'text="진행 중" bounds="[432,774][1026,879]"/>'
        '<node package="notion.id" class="android.view.View" '
        'text="작성일시" bounds="[60,888][420,993]"/>'
        '<node package="notion.id" class="android.view.View" '
        'text="25 February 2022 17:11" bounds="[432,888][1026,993]"/>'
        '<node package="notion.id" class="android.widget.Button" '
        'text="Add a property" clickable="true" '
        'bounds="[60,1002][1026,1107]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Tap here to continue…" bounds="[54,1356][1026,1527]"/>'
        '<node package="com.android.systemui" class="android.widget.FrameLayout" '
        'resource-id="com.android.systemui:id/navigation_bar_frame" '
        'bounds="[0,2256][1080,2400]"/>'
        "</hierarchy>"
    )


def nested_inventory():
    return {
        "page-000001": {
            "page_ref": "page-000001",
            "notion_page_id": PARENT_ID,
            "title": "Parent",
            "section": "private",
            "parent_page_ref": None,
        },
        "page-000001-subpage-000001": {
            "page_ref": "page-000001-subpage-000001",
            "notion_page_id": None,
            "title": "Child",
            "section": "private",
            "parent_page_ref": "page-000001",
        },
    }


def test_home_rows_preserve_uuid_section_and_nested_parent():
    rows = pages.parse_home_rows(home_tree())
    inventory, _ = pages.merge_rows({}, {}, rows)
    records = pages.validate_inventory(inventory)

    assert [
        (
            item["page_id"],
            item["section"],
            item["parent_page_id"],
            item["depth"],
        )
        for item in records
    ] == [
        (PARENT_ID, "private", None, 0),
        (CHILD_ID, "private", PARENT_ID, 1),
        (DATABASE_ID, "private", None, 0),
        (SHARED_ID, "shared", None, 0),
        (TEAM_ID, "teamspaces", None, 0),
    ]


def test_home_row_ignores_leaf_expansion_status_below_title():
    root = home_tree()
    parent = next(
        node
        for node in root.iter()
        if node.get("resource-id", "").endswith(PARENT_ID)
    )
    parent.append(
        ET.fromstring(
            '<node package="notion.id" '
            'class="android.widget.TextView" text="No pages inside" '
            'enabled="true" bounds="[180,2140][523,2212]"/>'
        )
    )

    record = next(
        item
        for item in pages.parse_home_rows(root)
        if item["page_id"] == PARENT_ID
    )
    assert record["title"] == "Parent"


def test_section_headers_expose_current_boundaries():
    assert pages.parse_section_headers(home_tree()) == (
        {"section": "private", "bounds": (0, 809, 1080, 953)},
        {"section": "shared", "bounds": (0, 1337, 1080, 1481)},
        {"section": "teamspaces", "bounds": (0, 1625, 1080, 1769)},
    )


def test_general_page_keeps_accessibility_rendered_text():
    assert pages.parse_page(general_page_tree(), "Parent") == {
        "classification": "general_page",
        "rendered_text": "Parent\n\nFirst paragraph\nChecklist item",
    }


def test_block_level_editor_is_a_general_page():
    assert pages.parse_page(
        block_editor_page_tree(), "AURA Attachment Test"
    ) == {
        "classification": "general_page",
        "rendered_text": "AURA Attachment Test\nhttps://example.com",
    }


def test_title_only_editor_is_an_empty_general_page():
    assert pages.parse_page(
        block_editor_page_tree(include_body=False),
        "AURA Attachment Test",
    ) == {
        "classification": "general_page",
        "rendered_text": "AURA Attachment Test",
    }


def test_short_general_page_accepts_observed_content_viewport_bottom():
    root = general_page_tree()
    document = next(
        node
        for node in root.iter()
        if node.get("class") == "android.widget.EditText"
        and node.get("clickable") == "false"
    )
    document.set("bounds", "[0,234][1080,2112]")

    assert pages.parse_page(root, "Parent")["classification"] == (
        "general_page"
    )


def test_general_page_accepts_collapsed_header_content_viewport():
    root = general_page_tree()
    document = next(
        node
        for node in root.iter()
        if node.get("class") == "android.widget.EditText"
        and node.get("clickable") == "false"
    )
    document.set("bounds", "[0,0][1080,2400]")

    assert pages.parse_page(root, "Parent")["classification"] == (
        "general_page"
    )


def test_short_page_uses_document_title_when_editor_is_absent():
    root = general_page_tree()
    title = next(
        node
        for node in root
        if node.get("class") == "android.widget.EditText"
        and node.get("clickable") == "true"
    )
    root.remove(title)
    document = next(
        node
        for node in root.iter()
        if node.get("class") == "android.widget.EditText"
    )
    document.set("text", "Grandchild\n\n")
    document.set("bounds", "[0,234][1080,2112]")

    assert pages.parse_page(root, "Grandchild") == {
        "classification": "general_page",
        "rendered_text": "Grandchild\n\n",
    }


def test_database_page_is_classified_without_general_text():
    assert pages.parse_page(database_page_tree(), "Tasks") == {
        "classification": "database",
    }


def test_block_database_item_accepts_empty_rendered_body():
    assert databases.parse_database_item(
        block_database_item_tree(),
        "엄마한테 전화하기",
    ) == {
        "title": "엄마한테 전화하기",
        "properties": [
            {"name": "Files & media", "value": "Empty", "ordinal": 1},
            {"name": "상태", "value": "진행 중", "ordinal": 2},
            {
                "name": "작성일시",
                "value": "25 February 2022 17:11",
                "ordinal": 3,
            },
        ],
        "rendered_body": "",
    }


class _OneMissBoardDevice:
    def __init__(self):
        self.offset = 0
        self.left_attempts = 0

    def hierarchy(self):
        cards = (
            (
                (120, 1326, 900, 1479, "First"),
                (984, 1326, 1080, 1479, ".. Second"),
            ),
            (
                (0, 1326, 96, 1479, "First"),
                (30, 1326, 813, 1479, ".. Second"),
            ),
        )[self.offset]
        card_xml = "".join(
            (
                '<node package="notion.id" class="android.view.View" '
                f'content-desc="{title}" clickable="true" focusable="true" '
                f'bounds="[{left},{top}][{right},{bottom}]"/>'
            )
            for left, top, right, bottom, title in cards
        )
        return (
            "<hierarchy>"
            '<node package="notion.id" class="android.widget.Button" '
            'content-desc="Filter and Sort" clickable="true" '
            'bounds="[612,1029][708,1128]"/>'
            '<node package="notion.id" class="android.widget.Button" '
            'content-desc="Edit view layout, grouping and more..." '
            'clickable="true" bounds="[708,1029][804,1128]"/>'
            f"{card_xml}"
            '<node package="com.android.systemui" '
            'class="android.widget.FrameLayout" '
            'resource-id="com.android.systemui:id/navigation_bar_frame" '
            'bounds="[0,2256][1080,2400]"/>'
            "</hierarchy>"
        )

    def swipe(self, direction, duration=0.2):
        assert direction in {"up", "down"}

    def swipe_bounds(
        self,
        value,
        direction,
        duration=0.2,
        distance_ratio=0.8,
    ):
        assert tuple(value) == (0, 1128, 1080, 2256)
        if direction == "right":
            self.offset = 0
            return
        assert direction == "left"
        self.left_attempts += 1
        if self.left_attempts > 1:
            self.offset = 1

    @staticmethod
    def screenshot():
        return b"\x89PNG\r\n\x1a\nnotion-board"


def test_find_card_rescans_once_after_positional_swipe_misses(tmp_path):
    device = _OneMissBoardDevice()
    journal = EventJournal(
        tmp_path / "events.jsonl",
        {"run_id": "notion-board-relocation"},
    )
    context = SimpleNamespace(
        device=device,
        journal=journal,
        ui=UiRuntime(
            device,
            journal,
            default_timeout=0.01,
            poll_interval=0.0,
        ),
        app_profile=SimpleNamespace(
            timings={"transition_settle": 0.0},
        ),
    )
    tree = device.hierarchy()
    observation = {
        "observation_id": "database-entry",
        "root": ET.fromstring(tree),
        "screen": device.screenshot(),
        "tree": tree.encode(),
    }

    row, _, _ = databases._find_card(
        context,
        count(1),
        "page-000001",
        observation,
        {
            "title": "Second",
            "horizontal_index": 1,
            "vertical_index": 0,
            "visible_index": 0,
            "same_title_occurrence": 1,
            "database_item_ref": "database-item-000002",
        },
    )

    assert row == {
        "title": "Second",
        "bounds": (30, 1326, 813, 1479),
    }


def test_inline_database_controls_do_not_override_general_page_text():
    assert pages.parse_page(
        general_page_tree(include_database_controls=True),
        "Parent",
    )["classification"] == "general_page"


def test_internal_subpage_blocks_use_accessibility_structure():
    root = ET.fromstring(
        "<hierarchy>"
        '<node package="notion.id" class="android.view.View" '
        'content-desc="Child" clickable="true" focusable="true" '
        'bounds="[72,720][1008,807]">'
        '<node package="notion.id" class="android.widget.Button" '
        'clickable="true" bounds="[72,720][144,792]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Child" bounds="[168,720][1008,807]"/>'
        "</node>"
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="Share" clickable="true" focusable="true" '
        'bounds="[900,99][1008,210]"/>'
        "</hierarchy>"
    )

    assert pages.parse_internal_subpages(root) == (
        {"title": "Child", "bounds": (72, 720, 1008, 807)},
    )


def test_internal_subpage_block_must_clear_navigation_bar():
    root = ET.fromstring(
        "<hierarchy>"
        '<node package="notion.id" class="android.view.View" '
        'content-desc="Child" clickable="true" focusable="true" '
        'bounds="[72,2196][1008,2283]">'
        '<node package="notion.id" class="android.widget.Button" '
        'clickable="true" bounds="[72,2196][144,2268]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Child" bounds="[168,2196][1008,2283]"/>'
        "</node>"
        '<node package="com.android.systemui" '
        'class="android.widget.FrameLayout" '
        'resource-id="com.android.systemui:id/navigation_bar_frame" '
        'bounds="[0,2256][1080,2400]"/>'
        "</hierarchy>"
    )

    assert pages.parse_internal_subpages(root) == ()


def test_internal_subpage_block_must_clear_floating_search_toolbar():
    root = ET.fromstring(
        "<hierarchy>"
        '<node package="notion.id" class="android.view.View" '
        'content-desc="리부탈 초안" clickable="true" focusable="true" '
        'bounds="[72,2151][1008,2238]">'
        '<node package="notion.id" class="android.widget.Button" '
        'clickable="true" bounds="[72,2151][144,2223]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="리부탈 초안" bounds="[168,2151][1008,2238]"/>'
        "</node>"
        '<node package="notion.id" class="android.view.View" '
        'resource-id="floating-toolbar.search-button" clickable="true" '
        'bounds="[48,2100][1032,2244]"/>'
        '<node package="com.android.systemui" '
        'class="android.widget.FrameLayout" '
        'resource-id="com.android.systemui:id/navigation_bar_frame" '
        'bounds="[0,2256][1080,2400]"/>'
        "</hierarchy>"
    )

    assert pages.parse_internal_subpages(root) == ()


def test_duplicate_titles_keep_distinct_uuid_identity():
    inventory = {
        PARENT_ID: {
            "page_id": PARENT_ID,
            "page_ref": f"page-{PARENT_ID}",
            "title": "Duplicate",
            "section": "private",
            "parent_page_id": None,
        },
        SHARED_ID: {
            "page_id": SHARED_ID,
            "page_ref": f"page-{SHARED_ID}",
            "title": "Duplicate",
            "section": "shared",
            "parent_page_id": None,
        },
    }

    assert [
        item["page_id"] for item in pages.validate_inventory(inventory)
    ] == [PARENT_ID, SHARED_ID]


def test_page_prefix_physically_nests_child_under_parent():
    assert pages.page_prefix(
        "notion/workspaces/workspace-000001 (Workspace)/pages",
        "page-000001-subpage-000001",
        nested_inventory(),
    ) == (
        "notion/workspaces/workspace-000001 (Workspace)/pages/"
        "page-000001 (Parent)/subpages/"
        "page-000001-subpage-000001 (Child)"
    )


def test_page_inventory_rejects_parent_cycle():
    inventory = {
        PARENT_ID: {
            "page_id": PARENT_ID,
            "title": "One",
            "section": "private",
            "parent_page_id": CHILD_ID,
        },
        CHILD_ID: {
            "page_id": CHILD_ID,
            "title": "Two",
            "section": "private",
            "parent_page_id": PARENT_ID,
        },
    }

    with pytest.raises(
        NotionCollectorError,
        match="Notion page inventory contains a cycle",
    ):
        pages.validate_inventory(inventory)


class NotionPageDevice:
    def __init__(
        self,
        *,
        wrong_parent_title=False,
        initially_scrolled=False,
        restore_scrolled_parent=False,
        canonical_page_unavailable=False,
        recent_page_unavailable=False,
    ):
        self.state = "home"
        self.page_stack = []
        self.swipes = []
        self.wrong_parent_title = wrong_parent_title
        self.initially_scrolled = initially_scrolled
        self.restore_scrolled_parent = restore_scrolled_parent
        self.canonical_page_unavailable = canonical_page_unavailable
        self.recent_page_unavailable = recent_page_unavailable
        self.page_scrolled = False
        self.scrolled_pages = set()
        self.bounded_swipes = []

    def exists(self, selector):
        return self.count(selector) > 0

    def count(self, selector):
        if selector == {"packageName": "notion.id"}:
            return 1
        if selector.get("resourceId") == "vision_tab_Home":
            return int(self.state == "home")
        return 0

    def click_bounds(self, value, *, anchor="center"):
        assert anchor == "center"
        value = tuple(value)
        if self.state == "home":
            if value == (60, 200, 456, 400):
                assert self.canonical_page_unavailable
                if self.recent_page_unavailable:
                    return
                self.page_stack = [PARENT_ID]
                self.page_scrolled = False
                self.state = "page"
                return
            rows = {
                (60, 450, 1020, 575): PARENT_ID,
                (60, 575, 1020, 700): DATABASE_ID,
                (60, 825, 1020, 950): SHARED_ID,
                (60, 1075, 1020, 1200): TEAM_ID,
            }
            try:
                page_id = rows[value]
            except KeyError:
                raise AssertionError(f"unexpected bounds: {value}") from None
            if self.canonical_page_unavailable and page_id == PARENT_ID:
                return
            self.page_stack = [page_id]
            self.page_scrolled = (
                self.initially_scrolled and page_id == PARENT_ID
            )
            self.state = "page"
            return
        if (
            self.state == "page"
            and self.page_stack[-1] == DATABASE_ID
            and value == (192, 1326, 972, 1479)
        ):
            self.state = "database_item"
            return
        transitions = {
            (PARENT_ID, (72, 720, 1008, 807)): CHILD_ID,
            (CHILD_ID, (72, 820, 1008, 907)): SUBCHILD_ID,
        }
        try:
            if self.restore_scrolled_parent:
                self.scrolled_pages.add(self.page_stack[-1])
            self.page_stack.append(
                transitions[(self.page_stack[-1], value)]
            )
            self.page_scrolled = False
        except KeyError:
            raise AssertionError(f"unexpected page bounds: {value}") from None

    def swipe(self, direction, duration=0.2):
        assert direction in {"up", "down", "left", "right"}
        self.swipes.append((self.state, direction))
        if self.state == "page" and direction == "down":
            self.page_scrolled = False
            self.scrolled_pages.discard(self.page_stack[-1])

    def swipe_bounds(
        self,
        value,
        direction,
        duration=0.2,
        distance_ratio=0.8,
    ):
        self.bounded_swipes.append(
            (self.state, direction, distance_ratio)
        )
        self.swipe(direction, duration)

    def back(self):
        if self.state == "database_item":
            self.state = "page"
            return
        assert self.state == "page"
        self.page_stack.pop()
        if not self.page_stack:
            self.state = "home"
            self.page_scrolled = False
        else:
            self.page_scrolled = (
                self.page_stack[-1] in self.scrolled_pages
            )

    def hierarchy(self):
        if self.state == "database_item":
            return self._database_item_xml()
        if self.state == "page":
            return self._page_xml()
        recent = (
            '<node package="notion.id" class="android.view.View" '
            'resource-id="home-tab.recents.page-row.Parent" '
            'clickable="true" enabled="true" '
            'bounds="[60,200][456,400]">'
            '<node package="notion.id" class="android.widget.TextView" '
            'text="Parent" enabled="true" bounds="[96,250][420,330]"/>'
            '</node>'
            if self.canonical_page_unavailable
            else ""
        )
        return (
            "<hierarchy>"
            '<node package="notion.id" class="android.view.View" '
            'resource-id="vision_tab_Home" selected="true" enabled="true" '
            'bounds="[204,80][460,224]"/>'
            '<node package="notion.id" class="android.view.View" '
            'resource-id="home-tab.sections.private-header" clickable="true" '
            'enabled="true" bounds="[0,325][1080,450]"/>'
            f"{recent}{_page_row('private', PARENT_ID, 'Parent', 450, 60)}"
            f"{_page_row('private', DATABASE_ID, 'Tasks', 575, 60)}"
            '<node package="notion.id" class="android.view.View" '
            'resource-id="home-tab.sections.shared-header" clickable="true" '
            'enabled="true" bounds="[0,700][1080,825]"/>'
            f"{_page_row('shared', SHARED_ID, 'Shared Page', 825, 60)}"
            + '<node package="notion.id" class="android.view.View" '
            'resource-id="home-tab.sections.teamspaces-header" '
            'clickable="true" enabled="true" '
            'bounds="[0,950][1080,1075]"/>'
            + _page_row(
                "teamspaces", TEAM_ID, "Team Page", 1075, 60
            )
            + "</hierarchy>"
        )

    def _page_xml(self):
        titles = {
            PARENT_ID: "Wrong" if self.wrong_parent_title else "Parent",
            CHILD_ID: "Child",
            SUBCHILD_ID: "Grandchild",
            DATABASE_ID: "Tasks",
            SHARED_ID: "Shared Page",
            TEAM_ID: "Team Page",
        }
        page_id = self.page_stack[-1]
        title = titles[page_id]
        if page_id == DATABASE_ID:
            body = (
                '<node package="notion.id" class="android.widget.EditText" '
                'text="Task database" clickable="true" '
                'bounds="[72,600][972,720]"/>'
                '<node package="notion.id" class="android.widget.Button" '
                'content-desc="Board View" clickable="true" '
                'bounds="[72,1029][360,1128]"/>'
                '<node package="notion.id" class="android.widget.Button" '
                'content-desc="Filter and Sort" clickable="true" '
                'bounds="[612,1029][708,1128]"/>'
                '<node package="notion.id" class="android.widget.Button" '
                'content-desc="Edit view layout, grouping and more..." '
                'clickable="true" bounds="[708,1029][804,1128]"/>'
                '<node package="notion.id" class="android.view.View" '
                'content-desc=".. Task" clickable="true" focusable="true" '
                'bounds="[192,1326][972,1479]"/>'
                '<node package="com.android.systemui" '
                'class="android.widget.FrameLayout" '
                'resource-id="com.android.systemui:id/navigation_bar_frame" '
                'bounds="[0,2256][1080,2400]"/>'
            )
        else:
            document_bounds = (
                "[0,0][1080,2400]"
                if self.page_scrolled
                else "[0,234][1080,2400]"
            )
            body = (
                '<node package="notion.id" class="android.widget.EditText" '
                f'text="{title}&#10;&#10;Rendered body" clickable="false" '
                f'bounds="{document_bounds}"/>'
            )
            if page_id == PARENT_ID:
                body += self._subpage("Child", 720, 807)
            elif page_id == CHILD_ID:
                body += self._subpage("Grandchild", 820, 907)
        title_bounds = (
            "[72,450][498,573]"
            if page_id == DATABASE_ID
            else "[54,492][1026,615]"
        )
        return (
            "<hierarchy>"
            '<node package="notion.id" class="android.widget.Button" '
            'content-desc="Back" clickable="true" '
            'bounds="[48,99][156,210]"/>'
            + (
                ""
                if self.page_scrolled or page_id == SUBCHILD_ID
                else (
                    '<node package="notion.id" '
                    'class="android.widget.EditText" '
                    f'text="{title}" clickable="true" '
                    f'bounds="{title_bounds}"/>'
                )
            )
            + f"{body}</hierarchy>"
        )

    @staticmethod
    def _database_item_xml():
        return (
            "<hierarchy>"
            '<node package="notion.id" class="android.widget.Button" '
            'content-desc="Back" clickable="true" '
            'bounds="[48,99][156,210]"/>'
            '<node package="notion.id" class="android.widget.EditText" '
            'text="Task" clickable="true" '
            'bounds="[54,492][1026,615]"/>'
            '<node package="notion.id" class="android.view.View" '
            'text="Status" bounds="[60,660][420,756]"/>'
            '<node package="notion.id" class="android.view.View" '
            'text="In progress" bounds="[432,660][1026,756]"/>'
            '<node package="notion.id" class="android.widget.Button" '
            'text="Add a property" clickable="true" '
            'bounds="[60,864][1026,969]"/>'
            '<node package="notion.id" class="android.widget.EditText" '
            'text="&#10;&#10;Task&#10;&#10;Status&#10;In progress'
            '&#10;Add a property&#10;&#10;Body&#10;New Task&#10;Empty'
            '&#10;New template" '
            'clickable="false" bounds="[0,234][1080,2400]"/>'
            "</hierarchy>"
        )

    @staticmethod
    def _subpage(title, top, bottom):
        return (
            '<node package="notion.id" class="android.view.View" '
            f'content-desc="{title}" clickable="true" focusable="true" '
            f'bounds="[72,{top}][1008,{bottom}]">'
            '<node package="notion.id" class="android.widget.Button" '
            f'clickable="true" bounds="[72,{top}][144,{top + 72}]"/>'
            '<node package="notion.id" class="android.widget.TextView" '
            f'text="{title}" bounds="[168,{top}][1008,{bottom}]"/>'
            "</node>"
        )

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nnotion-page"


class CarouselPageDevice(NotionPageDevice):
    def __init__(self, heading):
        super().__init__()
        self.heading = heading
        self.carousel_offset = 0
        self.boundary_swipes = []

    def _visible_carousel_cards(self):
        return (
            (
                (60, 200, 456, 400, "Tasks"),
                (504, 200, 1080, 400, "offline-only"),
            ),
            (
                (0, 200, 456, 400, "offline-only"),
                (504, 200, 1020, 400, "Parent"),
            ),
        )[self.carousel_offset]

    def click_bounds(self, value, *, anchor="center"):
        value = tuple(value)
        visible = {
            item[:4]: item[4]
            for item in self._visible_carousel_cards()
        }
        if self.state == "home" and value in visible:
            title = visible[value]
            page_id = {
                "Tasks": DATABASE_ID,
                "Parent": PARENT_ID,
                "offline-only": "offline-only",
            }[title]
            self.page_stack = [page_id]
            self.page_scrolled = False
            self.state = "page"
            return
        super().click_bounds(value, anchor=anchor)

    def swipe_bounds(
        self,
        value,
        direction,
        duration=0.2,
        distance_ratio=0.8,
    ):
        if self.state != "home":
            return super().swipe_bounds(
                value,
                direction,
                duration,
                distance_ratio,
            )
        assert self.state == "home"
        assert tuple(value) == (
            (60, 200, 1080, 400),
            (0, 200, 1020, 400),
        )[self.carousel_offset]
        assert direction in {"left", "right"}
        self.boundary_swipes.append(direction)
        if direction == "left":
            self.carousel_offset = 1
        else:
            self.carousel_offset = 0

    def hierarchy(self):
        if self.state == "page" and self.page_stack[-1] == "offline-only":
            return (
                "<hierarchy>"
                '<node package="notion.id" class="android.widget.Button" '
                'content-desc="Back" clickable="true" '
                'bounds="[48,99][156,210]"/>'
                '<node package="notion.id" '
                'class="android.widget.EditText" text="offline-only" '
                'clickable="true" bounds="[54,492][1026,615]"/>'
                '<node package="notion.id" '
                'class="android.widget.EditText" '
                'text="offline-only&#10;&#10;Cached body" '
                'clickable="false" bounds="[0,234][1080,2400]"/>'
                "</hierarchy>"
            )
        if self.state != "home":
            return super().hierarchy()
        cards = []
        for left, top, right, bottom, title in (
            self._visible_carousel_cards()
        ):
            cards.append(
                '<node package="notion.id" class="android.view.View" '
                f'resource-id="home-tab.recents.page-row.{title}" '
                'clickable="true" enabled="true" '
                f'bounds="[{left},{top}][{right},{bottom}]">'
                '<node package="notion.id" '
                'class="android.widget.TextView" '
                f'text="{title}" enabled="true" '
                f'bounds="[{left + 20},250][{right - 20},330]"/>'
                "</node>"
            )
        return (
            "<hierarchy>"
            '<node package="notion.id" class="android.view.View" '
            'resource-id="vision_tab_Home" selected="true" enabled="true" '
            'bounds="[204,80][460,224]"/>'
            '<node package="notion.id" class="android.view.View" '
            'resource-id="home-tab.sections.recent-header" '
            'clickable="true" enabled="true" bounds="[0,80][1080,200]">'
            '<node package="notion.id" class="android.widget.TextView" '
            f'text="{self.heading}" bounds="[60,100][500,180]"/>'
            "</node>"
            + "".join(cards)
            + '<node package="notion.id" class="android.view.View" '
            'resource-id="home-tab.sections.private-header" '
            'clickable="true" enabled="true" bounds="[0,400][1080,450]"/>'
            + _page_row("private", PARENT_ID, "Parent", 450, 60)
            + _page_row("private", DATABASE_ID, "Tasks", 575, 60)
            + '<node package="notion.id" class="android.view.View" '
            'resource-id="home-tab.sections.shared-header" '
            'clickable="true" enabled="true" bounds="[0,700][1080,825]"/>'
            + _page_row("shared", SHARED_ID, "Shared Page", 825, 60)
            + '<node package="notion.id" class="android.view.View" '
            'resource-id="home-tab.sections.teamspaces-header" '
            'clickable="true" enabled="true" bounds="[0,950][1080,1075]"/>'
            + _page_row("teamspaces", TEAM_ID, "Team Page", 1075, 60)
            + "</hierarchy>"
        )

    def back(self):
        was_page = self.state == "page"
        super().back()
        if was_page and self.state == "home":
            self.carousel_offset = 0


class CollapsedCarouselDevice(CarouselPageDevice):
    def __init__(self):
        super().__init__("Offline pages")
        self.expanded = False
        self.header_clicks = 0

    def click_bounds(self, value, *, anchor="center"):
        if self.state == "home" and tuple(value) == (0, 80, 1080, 200):
            self.expanded = True
            self.header_clicks += 1
            return
        super().click_bounds(value, anchor=anchor)

    def hierarchy(self):
        xml = super().hierarchy()
        if self.state != "home" or self.expanded:
            return xml
        root = ET.fromstring(xml)
        for node in tuple(root):
            if node.get("resource-id", "").startswith(
                "home-tab.recents.page-row."
            ):
                root.remove(node)
        return ET.tostring(root, encoding="unicode")


class PagerEscapingCarouselDevice(CarouselPageDevice):
    def __init__(self):
        super().__init__("Offline pages")
        self.tab_escapes = 0

    def swipe_bounds(
        self,
        value,
        direction,
        duration=0.2,
        distance_ratio=0.8,
    ):
        if (
            self.state == "home"
            and direction == "left"
            and self.carousel_offset == 1
        ):
            self.state = "chats"
            self.tab_escapes += 1
            return
        super().swipe_bounds(
            value,
            direction,
            duration,
            distance_ratio,
        )

    def back(self):
        if self.state == "chats":
            self.state = "home"
            return
        super().back()

    def hierarchy(self):
        if self.state == "chats":
            return (
                "<hierarchy>"
                '<node package="notion.id" class="android.view.View" '
                'resource-id="vision_tab_Home" selected="false" '
                'bounds="[204,80][460,224]"/>'
                '<node package="notion.id" class="android.view.View" '
                'resource-id="vision_tab_Chats" selected="true" '
                'bounds="[484,80][740,224]"/>'
                "</hierarchy>"
            )
        return super().hierarchy()


class LoadingPageDevice(NotionPageDevice):
    def __init__(self, *, resolves=False):
        super().__init__()
        self.resolves = resolves
        self.loading_polls = 0

    def click_bounds(self, value, *, anchor="center"):
        if self.state == "home" and tuple(value) == (60, 825, 1020, 950):
            self.state = "loading"
            return
        super().click_bounds(value, anchor=anchor)

    def swipe(self, direction, duration=0.2):
        if self.state == "loading":
            self.swipes.append((self.state, direction))
            return
        super().swipe(direction, duration)

    def back(self):
        if self.state == "loading":
            self.state = "home"
            return
        super().back()

    def hierarchy(self):
        if self.state == "loading":
            self.loading_polls += 1
            if self.resolves and self.loading_polls >= 4:
                self.state = "page"
                self.page_stack = [SHARED_ID]
                return super().hierarchy()
            return (
                "<hierarchy>"
                '<node package="notion.id" class="android.view.View" '
                'content-desc="Loading" bounds="[420,1080][660,1320]"/>'
                '<node package="notion.id" class="android.widget.EditText" '
                'text="Ask AI" clickable="false" '
                'bounds="[222,2196][858,2304]"/>'
                "</hierarchy>"
            )
        return super().hierarchy()


class LoadErrorPageDevice(LoadingPageDevice):
    def hierarchy(self):
        if self.state != "loading":
            return super().hierarchy()
        self.loading_polls += 1
        loading = (
            '<node package="notion.id" class="android.view.View" '
            'content-desc="Loading" bounds="[420,1080][660,1320]"/>'
            if self.loading_polls < 4
            else ""
        )
        return (
            "<hierarchy>"
            '<node package="notion.id" class="android.widget.Button" '
            'content-desc="Back" clickable="true" '
            'bounds="[48,99][156,210]"/>'
            '<node package="notion.id" class="android.widget.Button" '
            'text="Shared Page" clickable="true" '
            'bounds="[168,99][768,210]"/>'
            '<node package="notion.id" class="android.widget.TextView" '
            'text="Oops, there was an error loading this page." '
            'bounds="[81,1044][999,1116]"/>'
            '<node package="notion.id" class="android.widget.TextView" '
            'text="Refresh to load it again." '
            'bounds="[285,1116][795,1188]"/>'
            f"{loading}</hierarchy>"
        )


def run_page_fixture(
    tmp_path,
    *,
    wrong_parent_title=False,
    initially_scrolled=False,
    restore_scrolled_parent=False,
    canonical_page_unavailable=False,
    recent_page_unavailable=False,
    device=None,
):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    journal = EventJournal(
        run_dir / "events.jsonl",
        {"run_id": "notion-pages"},
    )
    artifacts = ArtifactStore(run_dir, journal)
    device = device or NotionPageDevice(
        wrong_parent_title=wrong_parent_title,
        initially_scrolled=initially_scrolled,
        restore_scrolled_parent=restore_scrolled_parent,
        canonical_page_unavailable=canonical_page_unavailable,
        recent_page_unavailable=recent_page_unavailable,
    )
    profile = ProfileStore(ROOT / "profiles").load_app(
        "notion", "0.6.4030"
    )
    profile = replace(
        profile,
        timings={
            "default_timeout": 0.01,
            "poll_interval": 0.0,
            "transition_settle": 0.0,
            "application_start_settle": 0.0,
        },
    )
    context = RunContext(
        run_dir=run_dir,
        base_context={"run_id": "notion-pages"},
        device=device,
        journal=journal,
        artifacts=artifacts,
        ui=UiRuntime(
            device,
            journal,
            default_timeout=0.01,
            poll_interval=0.0,
        ),
        app_profile=profile,
        system_ui_profile=ProfileStore(ROOT / "profiles").load_system_ui(
            "samsung"
        ),
    )
    report = pages.collect_workspace_pages(
        context,
        {"workspace_ref": "workspace-000001"},
        "notion/workspaces/workspace-000001 (Workspace)",
        "account.notion.test.001",
    )
    return report, context


def test_collect_workspace_pages_recurses_and_retains_nested_outputs(tmp_path):
    report, context = run_page_fixture(tmp_path)

    assert report["status"] == "complete"
    assert report["page_count"] == 5
    assert report["database_count"] == 1
    assert report["database_item_count"] == 1
    assert report["database_out_of_scope_count"] == 0
    assert report["attachment_count"] == 0
    assert report["attachment_materialized_count"] == 0
    assert report["attachment_out_of_scope_count"] == 0
    assert report["discovered_page_count"] == 6
    assert ("page", "left", 0.8) in context.device.bounded_swipes

    paths = {record.relative_path for record in context.artifacts.records}
    parent = (
        "artifacts/notion/workspaces/workspace-000001 (Workspace)/pages/"
        "page-000001 (Parent)"
    )
    child = (
        f"{parent}/subpages/"
        "page-000001-subpage-000001 (Child)"
    )
    grandchild = (
        f"{child}/subpages/"
        "page-000001-subpage-000001-subpage-000001 (Grandchild)"
    )
    assert {f"{parent}/page.json", f"{parent}/page.png", f"{parent}/page.xml"} <= paths
    assert {f"{child}/page.json", f"{child}/page.png", f"{child}/page.xml"} <= paths
    assert {
        f"{grandchild}/page.json",
        f"{grandchild}/page.png",
        f"{grandchild}/page.xml",
    } <= paths
    database_prefix = (
        "artifacts/notion/workspaces/workspace-000001 (Workspace)/pages/"
        "page-000002 (Tasks)"
    )
    assert {
        f"{database_prefix}/database.json",
        f"{database_prefix}/database.png",
        f"{database_prefix}/database.xml",
        (
            f"{database_prefix}/items/"
            "database-item-000001 (Task)/item.json"
        ),
        (
            f"{database_prefix}/items/"
            "database-item-000001 (Task)/item.png"
        ),
        (
            f"{database_prefix}/items/"
            "database-item-000001 (Task)/item.xml"
        ),
    } <= paths

    inventory = next(
        context.run_dir.glob("artifacts/notion/workspaces/*/pages.json")
    )
    database = next(
        item
        for item in __import__("json").loads(
            inventory.read_text(encoding="utf-8")
        )["pages"]
        if item["notion_page_id"] == DATABASE_ID
    )
    assert {
        key: database[key]
        for key in (
            "classification",
            "reason_code",
            "acquisition_status",
            "item_count",
        )
    } == {
        "classification": "database",
        "reason_code": None,
        "acquisition_status": "complete",
        "item_count": 1,
    }
    child_record = next(
        item
        for item in __import__("json").loads(
            inventory.read_text(encoding="utf-8")
        )["pages"]
        if item["title"] == "Child"
    )
    assert child_record["notion_page_id"] is None
    assert child_record["parent_page_ref"] == "page-000001"


def test_collect_workspace_pages_reports_title_mismatch(tmp_path):
    with pytest.raises(pages.PageCollectionError) as raised:
        run_page_fixture(tmp_path, wrong_parent_title=True)

    assert raised.value.reason_code == "notion_page_title_unverified"
    assert raised.value.page_id == PARENT_ID
    assert raised.value.page_ref == "page-000001"
    assert raised.value.report["discovered_page_count"] == 4
    assert len(raised.value.report["pages"]) == 4


def test_collect_rewinds_restored_page_position_before_verification(tmp_path):
    report, context = run_page_fixture(
        tmp_path,
        initially_scrolled=True,
    )

    assert report["status"] == "complete"
    assert ("page", "down") in context.device.swipes


def test_collect_uses_unique_recent_fallback_when_canonical_row_does_not_open(
    tmp_path,
):
    report, context = run_page_fixture(
        tmp_path,
        canonical_page_unavailable=True,
    )

    assert report["status"] == "complete"
    assert report["page_count"] == 5
    assert context.device.state == "home"


def test_unavailable_page_entry_does_not_abort_remaining_pages(tmp_path):
    report, context = run_page_fixture(
        tmp_path,
        canonical_page_unavailable=True,
        recent_page_unavailable=True,
    )

    assert report["status"] == "partial"
    assert report["reason_code"] == "notion_page_entry_unavailable"
    assert report["discovered_page_count"] == 4
    assert report["page_count"] == 2
    document = json.loads(next(
        context.run_dir.rglob("pages.json")
    ).read_text(encoding="utf-8"))
    assert [item["acquisition_status"] for item in document["pages"]] == [
        "partial",
        "complete",
        "complete",
        "complete",
    ]
    assert document["pages"][0]["reason_code"] == (
        "notion_page_entry_unavailable"
    )
    assert context.device.state == "home"


def test_named_carousel_is_collected_independently(
    tmp_path,
):
    report, context = run_page_fixture(
        tmp_path,
        device=CarouselPageDevice("Offline pages"),
    )

    assert report["status"] == "complete"
    assert report["reason_code"] is None
    document = json.loads(next(
        context.run_dir.rglob("pages.json")
    ).read_text(encoding="utf-8"))
    cards = [
        item for item in document["pages"]
        if item["discovery_source"] == "offline_pages"
        and item["parent_page_ref"] is None
    ]
    assert [item["title"] for item in cards] == [
        "Tasks",
        "offline-only",
        "Parent",
    ]
    assert all(
        item["page_ref"].startswith("offline-page-") for item in cards
    )
    assert all(item["notion_page_id"] is None for item in cards)
    assert [item["title"] for item in document["pages"]].count("Tasks") == 2
    assert [item["title"] for item in document["pages"]].count("Parent") == 2
    assert context.device.boundary_swipes == ["left", "right", "left"]
    assert context.device.state == "home"


def test_recents_carousel_is_not_collected(tmp_path):
    report, context = run_page_fixture(
        tmp_path,
        device=CarouselPageDevice("Recents"),
    )

    assert report["status"] == "complete"
    document = json.loads(next(
        context.run_dir.rglob("pages.json")
    ).read_text(encoding="utf-8"))
    assert not any(
        item["discovery_source"] == "recents"
        for item in document["pages"]
    )
    assert context.device.boundary_swipes == []


def test_collapsed_carousel_is_expanded_before_inventory(tmp_path):
    report, context = run_page_fixture(
        tmp_path,
        device=CollapsedCarouselDevice(),
    )

    assert report["status"] == "complete"
    document = json.loads(next(
        context.run_dir.rglob("pages.json")
    ).read_text(encoding="utf-8"))
    assert [
        item["title"] for item in document["pages"]
        if item["discovery_source"] == "offline_pages"
    ] == ["Tasks", "offline-only", "Parent"]
    assert context.device.header_clicks == 1


def test_carousel_end_is_detected_before_tab_pager_escape(tmp_path):
    report, context = run_page_fixture(
        tmp_path,
        device=PagerEscapingCarouselDevice(),
    )

    assert report["status"] == "complete"
    document = json.loads(next(
        context.run_dir.rglob("pages.json")
    ).read_text(encoding="utf-8"))
    cards = [
        item for item in document["pages"]
        if item["discovery_source"] == "offline_pages"
        and item["parent_page_ref"] is None
    ]
    assert [item["title"] for item in cards] == [
        "Tasks",
        "offline-only",
        "Parent",
    ]
    assert context.device.tab_escapes == 0
    assert context.device.state == "home"


def test_titleless_loading_page_is_unavailable_and_does_not_abort(tmp_path):
    report, context = run_page_fixture(
        tmp_path,
        device=LoadingPageDevice(),
    )

    assert report["status"] == "partial"
    assert report["reason_code"] == "notion_page_entry_unavailable"
    document = json.loads(next(
        context.run_dir.rglob("pages.json")
    ).read_text(encoding="utf-8"))
    shared = next(
        item for item in document["pages"] if item["title"] == "Shared Page"
    )
    team = next(
        item for item in document["pages"] if item["title"] == "Team Page"
    )
    assert shared["acquisition_status"] == "partial"
    assert shared["reason_code"] == "notion_page_entry_unavailable"
    assert team["acquisition_status"] == "complete"
    assert context.device.state == "home"


def test_loading_page_is_collected_when_it_resolves(tmp_path):
    report, context = run_page_fixture(
        tmp_path,
        device=LoadingPageDevice(resolves=True),
    )

    assert report["status"] == "complete"
    document = json.loads(next(
        context.run_dir.rglob("pages.json")
    ).read_text(encoding="utf-8"))
    shared = next(
        item for item in document["pages"] if item["title"] == "Shared Page"
    )
    assert shared["acquisition_status"] == "complete"
    assert context.device.state == "home"


def test_explicit_load_error_is_unavailable_and_does_not_abort(tmp_path):
    report, context = run_page_fixture(
        tmp_path,
        device=LoadErrorPageDevice(),
    )

    assert report["status"] == "partial"
    assert report["reason_code"] == "notion_page_entry_unavailable"
    document = json.loads(next(
        context.run_dir.rglob("pages.json")
    ).read_text(encoding="utf-8"))
    shared = next(
        item for item in document["pages"] if item["title"] == "Shared Page"
    )
    team = next(
        item for item in document["pages"] if item["title"] == "Team Page"
    )
    assert shared["acquisition_status"] == "partial"
    assert shared["reason_code"] == "notion_page_entry_unavailable"
    assert team["acquisition_status"] == "complete"
    assert ("loading", "down") not in context.device.swipes
    assert context.device.state == "home"


def test_collect_rewinds_restored_parent_after_descendant(tmp_path):
    report, context = run_page_fixture(
        tmp_path,
        restore_scrolled_parent=True,
    )

    assert report["status"] == "complete"
    assert ("page", "down") in context.device.swipes


def _attachment_page_tree(viewport):
    title = (
        '<node package="notion.id" class="android.widget.EditText" '
        'text="Attachment Page" clickable="true" focusable="true" '
        'bounds="[54,234][1026,480]"/>'
    )
    file_one = (
        '<node package="notion.id" class="android.widget.Button" '
        'text="duplicate.txt 54 B" clickable="true" focusable="true" '
        'bounds="[72,543][1008,636]"/>'
    )
    file_two = (
        '<node package="notion.id" class="android.widget.Button" '
        'text="duplicate.txt 54 B" clickable="true" focusable="true" '
        'bounds="[72,663][1008,756]"/>'
    )
    external = (
        '<node package="notion.id" class="android.widget.EditText" '
        'text="https://example.com" clickable="true" focusable="true" '
        'bounds="[72,423][1008,510]"/>'
    )
    image = (
        '<node package="notion.id" class="android.widget.Image" '
        'bounds="[114,795][966,1278]"/>'
    )
    video = (
        '<node package="notion.id" class="android.view.View" '
        'focusable="true" bounds="[78,600][1002,1500]">'
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="play" clickable="true" '
        'bounds="[372,900][708,1239]"/>'
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="enter full screen" clickable="true" '
        'bounds="[714,1320][858,1467]"/>'
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="Open block actions menu" clickable="true" '
        'focusable="true" bounds="[912,621][984,696]"/>'
        "</node>"
    )
    audio = (
        '<node package="notion.id" class="android.view.View" '
        'focusable="true" bounds="[78,1530][1002,1725]">'
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="play" clickable="true" focusable="true" '
        'bounds="[108,1563][204,1662]"/>'
        '<node package="notion.id" class="android.widget.SeekBar" '
        'clickable="true" bounds="[420,1563][780,1662]"/>'
        '<node package="notion.id" class="android.widget.Button" '
        'content-desc="mute" clickable="true" '
        'bounds="[780,1563][876,1662]"/>'
        '<node package="notion.id" class="android.widget.Button" '
        'clickable="true" focusable="true" '
        'bounds="[876,1563][972,1662]"/>'
        "</node>"
    )
    contents = {
        0: external + file_one + file_two,
        1: file_one + image,
        2: video + audio,
    }[viewport]
    return ET.fromstring(f"<hierarchy>{title}{contents}</hierarchy>")


class _AttachmentPageDevice:
    def __init__(self):
        self.viewport = 0

    def hierarchy(self):
        return ET.tostring(
            _attachment_page_tree(self.viewport),
            encoding="unicode",
        )

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nnotion-page-attachments"

    def swipe(self, direction, duration=0.2):
        assert direction == "up"
        self.viewport = min(2, self.viewport + 1)


def test_scan_page_content_collects_overlap_once(
    tmp_path,
    monkeypatch,
):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    journal = EventJournal(
        run_dir / "events.jsonl",
        {"run_id": "notion-page-attachments"},
    )
    device = _AttachmentPageDevice()
    context = SimpleNamespace(
        run_dir=run_dir,
        base_context={"run_id": "notion-page-attachments"},
        device=device,
        journal=journal,
        artifacts=ArtifactStore(run_dir, journal),
        ui=UiRuntime(
            device,
            journal,
            default_timeout=0.01,
            poll_interval=0.0,
        ),
        app_profile=SimpleNamespace(
            timings={"transition_settle": 0.0},
        ),
    )

    def retain_candidate(
        _context,
        _sequence,
        *,
        candidate,
        attachment_ref,
        observation,
        entry_action,
        **_kwargs,
    ):
        return entry_action, dict(observation), {
            "attachment_ref": attachment_ref,
            "kind": candidate["kind"],
            "displayed_name": candidate["displayed_name"],
            "acquisition_status": (
                "out_of_scope"
                if candidate["kind"] == "external_url"
                else "materialized"
            ),
        }

    monkeypatch.setattr(
        pages,
        "materialize_candidate",
        retain_candidate,
        raising=False,
    )
    children, records, _ = pages._scan_page_content(
        context,
        count(1),
        page={
            "page_ref": "page-000001",
            "title": "Attachment Page",
        },
        page_prefix="notion/pages/page-000001 (Attachment Page)",
        linked={"page_ref": "page-000001"},
    )
    assert children == ()
    assert [row["attachment_ref"] for row in records] == [
        "attachment-000001",
        "attachment-000002",
        "attachment-000003",
        "attachment-000004",
        "attachment-000005",
        "attachment-000006",
    ]
    assert [
        row["displayed_name"]
        for row in records
        if row["kind"] == "file"
    ] == ["duplicate.txt", "duplicate.txt"]
    assert sum(
        row["acquisition_status"] == "out_of_scope"
        for row in records
    ) == 1


def test_not_materialized_attachment_makes_page_partial_and_continues(
    tmp_path,
    monkeypatch,
):
    original = pages._scan_page_content

    def partial_scan(context, sequence, *, page, **kwargs):
        if page["title"] != "Parent":
            return original(
                context,
                sequence,
                page=page,
                **kwargs,
            )
        action = context.journal.record_action(
            "attachment_scan",
            status="success",
            context={"page_ref": page["page_ref"]},
        )
        return (), ({
            "attachment_ref": "attachment-000001",
            "kind": "file",
            "displayed_name": "missing.txt",
            "acquisition_status": "not_materialized",
            "reason_code": (
                "notion_attachment_inventory_did_not_settle"
            ),
        },), action

    monkeypatch.setattr(pages, "_scan_page_content", partial_scan)
    report, context = run_page_fixture(tmp_path)

    assert report["status"] == "partial"
    assert report["reason_code"] == (
        "notion_attachment_materialize_partial"
    )
    document = json.loads(next(
        context.run_dir.rglob("pages.json")
    ).read_text(encoding="utf-8"))
    record = next(
        row
        for row in document["pages"]
        if row["page_ref"] == "page-000001"
    )
    assert record["acquisition_status"] == "partial"
    assert record["attachment_count"] == 1
    assert record["attachment_materialized_count"] == 0
    assert report["discovered_page_count"] == 4
    assert report["page_count"] == 2


def test_attachment_restore_failure_is_page_failure(
    tmp_path,
    monkeypatch,
):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    journal = EventJournal(
        run_dir / "events.jsonl",
        {"run_id": "notion-page-attachment-restore"},
    )
    device = _AttachmentPageDevice()
    context = SimpleNamespace(
        run_dir=run_dir,
        device=device,
        journal=journal,
        artifacts=ArtifactStore(run_dir, journal),
        ui=UiRuntime(
            device,
            journal,
            default_timeout=0.01,
            poll_interval=0.0,
        ),
        app_profile=SimpleNamespace(
            timings={"transition_settle": 0.0},
        ),
    )

    def fail_restore(*args, entry_action, **kwargs):
        raise attachments.AttachmentCollectionError(
            "notion_attachment_parent_restore_failed",
            action=entry_action,
        )

    monkeypatch.setattr(
        pages,
        "materialize_candidate",
        fail_restore,
    )
    with pytest.raises(pages.PageCollectionError) as raised:
        pages._scan_page_content(
            context,
            count(1),
            page={
                "page_ref": "page-000001",
                "title": "Attachment Page",
            },
            page_prefix="notion/pages/page-000001",
            linked={"page_ref": "page-000001"},
        )
    assert raised.value.reason_code == (
        "notion_attachment_parent_restore_failed"
    )
