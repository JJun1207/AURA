import importlib
import json
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from aura.models import Route
from aura.profiles import ProfileError, ProfileStore
from aura.runtime import AcquisitionRuntime


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("partial", [False, True])
def test_account_email_headers_have_individual_common_bindings(tmp_path, partial):
    from aura.apps.notion import collect

    class MultiAccountDevice(NotionDevice):
        def hierarchy(self):
            value = super().hierarchy()
            if self.state == "switcher":
                marker = '<node package="notion.id" class="android.view.View" clickable="true" focusable="true" enabled="true" bounds="[40,250][1040,350]">'
                value = value.replace(marker, '<node package="notion.id" class="android.widget.TextView" text="second@example.test" enabled="true" bounds="[40,242][800,249]"/>' + marker)
            return value

    store = ProfileStore(ROOT / "profiles")
    profile = replace(store.load_app("notion", "0.6.4030"), timings={"default_timeout": .01, "poll_interval": 0, "transition_settle": 0, "application_start_settle": 0})
    device = MultiAccountDevice(ambiguous_members_for={"workspace-000002"} if partial else ())
    result = AcquisitionRuntime(tmp_path, device).run(
        profile, store.load_system_ui("samsung"), Route.MATERIALIZE,
        {"target": {"kind": "account", "ref": "account.test"}}, collect,
        run_id="multiple-accounts",
    )
    assert result.outcome.status.value == ("partial" if partial else "complete")
    accounts = [item for item in result.items if item.item_type == "account"]
    assert len(accounts) == 2
    artifact = next(item for item in result.artifacts if item.relative_path.endswith("/account.json"))
    original = json.loads((result.run_dir / artifact.relative_path).read_text())
    assert len(original["accounts"]) == 2
    for index, item in enumerate(accounts):
        assert item.source == {"collection_attempt_id": artifact.attempt_id, "artifact_id": artifact.artifact_id, "record_index": index, "record_key": "accounts"}
        assert not any(attempt.acquisition_item_id == item.acquisition_item_id for attempt in result.attempts)
    document = json.loads((result.run_dir / "acquisition.json").read_text())
    content = next(item for item in document["identifications"] if item["target_id"] == "notion.content")
    assert content["identified_item_count"] == 4  # Two accounts and two workspaces.


def _collector():
    try:
        return importlib.import_module("aura.apps.notion.collector")
    except ModuleNotFoundError:
        return None


def switcher_tree():
    return ET.fromstring(
        '<hierarchy>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Accounts" enabled="true" bounds="[400,40][680,100]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="first@example.test" enabled="true" '
        'bounds="[40,100][800,145]"/>'
        '<node package="notion.id" class="android.view.View" '
        'clickable="true" focusable="true" enabled="true" '
        'bounds="[40,150][1040,250]">'
        '<node package="notion.id" class="android.view.View" enabled="true" '
        'bounds="[60,165][140,235]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Shared" enabled="true" bounds="[180,165][500,205]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Free plan" enabled="true" bounds="[180,205][500,240]"/>'
        '</node>'
        '<node package="notion.id" class="android.view.View" '
        'clickable="true" focusable="true" enabled="true" '
        'bounds="[40,250][1040,350]">'
        '<node package="notion.id" class="android.view.View" enabled="true" '
        'bounds="[60,265][140,335]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Research" enabled="true" bounds="[180,265][500,305]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Plus plan" enabled="true" bounds="[180,305][500,340]"/>'
        '<node package="notion.id" class="android.view.View" enabled="true" '
        'bounds="[920,265][1000,335]"/>'
        '</node>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="second@example.test" enabled="true" '
        'bounds="[40,400][800,445]"/>'
        '<node package="notion.id" class="android.view.View" '
        'clickable="true" focusable="true" enabled="true" '
        'bounds="[40,450][1040,550]">'
        '<node package="notion.id" class="android.view.View" enabled="true" '
        'bounds="[60,465][140,535]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Shared" enabled="true" bounds="[180,465][500,505]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Free plan" enabled="true" bounds="[180,505][500,540]"/>'
        '</node>'
        '<node package="notion.id" class="android.view.View" '
        'resource-id="home-tab.account-switcher.create-workspace-test" '
        'clickable="true" enabled="true" bounds="[40,600][1040,700]">'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="New workspace" enabled="true" bounds="[180,625][500,675]"/>'
        '</node>'
        '<node package="notion.id" class="android.view.View" '
        'resource-id="home-tab.account-switcher.addAccount" '
        'clickable="true" enabled="true" bounds="[40,720][1040,820]">'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Add an account" enabled="true" bounds="[180,745][500,795]"/>'
        '</node>'
        '</hierarchy>'
    )


def members_tree():
    return ET.fromstring(
        '<hierarchy>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Members" enabled="true" bounds="[400,40][680,100]"/>'
        '<node package="notion.id" class="android.widget.EditText" '
        'text="https://app.notion.com/invite/example-token" enabled="false" '
        'bounds="[80,160][1000,220]"/>'
        '<node package="notion.id" class="android.view.View" enabled="true" '
        'bounds="[0,300][1080,460]">'
        '<node package="notion.id" class="android.widget.Image" '
        'content-desc="Analyst" enabled="true" bounds="[30,330][110,410]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Analyst (You)" enabled="true" bounds="[130,310][400,355]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Workspace owner" enabled="true" bounds="[410,310][650,355]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="analyst@example.test" enabled="true" '
        'bounds="[130,360][600,420]"/>'
        '<node package="notion.id" class="android.widget.Button" '
        'text="Workspace owner" clickable="true" enabled="true" '
        'bounds="[660,330][1040,410]"/>'
        '</node>'
        '<node package="notion.id" class="android.view.View" enabled="true" '
        'bounds="[0,460][1080,620]">'
        '<node package="notion.id" class="android.widget.Image" '
        'content-desc="Reviewer" enabled="true" bounds="[30,490][110,570]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Reviewer" enabled="true" bounds="[130,470][400,515]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Member" enabled="true" bounds="[410,470][650,515]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="reviewer@example.test" enabled="true" '
        'bounds="[130,520][600,580]"/>'
        '<node package="notion.id" class="android.widget.Button" '
        'text="Member" clickable="true" enabled="true" '
        'bounds="[660,490][1040,570]"/>'
        '</node>'
        '</hierarchy>'
    )


def offline_members_tree():
    return ET.fromstring(
        '<hierarchy>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Members" enabled="true" bounds="[360,78][720,210]"/>'
        '<node package="notion.id" class="android.widget.TextView" '
        'text="Please go online to manage settings." enabled="true" '
        'bounds="[0,210][1080,282]"/>'
        '</hierarchy>'
    )


class NotionDevice:
    def __init__(
        self,
        *,
        ambiguous_members_for=None,
        offline_members_for=None,
        members_noop_once_for=None,
        home_recompose_polls=0,
        workspace_recompose_polls=0,
        switcher_recompose_polls=0,
        members_recompose_polls=0,
        back_recompose_polls=0,
    ):
        self.state = "home"
        self.selected = "workspace-000001"
        self.ambiguous_members_for = set(ambiguous_members_for or ())
        self.offline_members_for = set(offline_members_for or ())
        self.members_noop_once_for = set(members_noop_once_for or ())
        self.members_click_counts = Counter()
        self.home_recompose_polls = home_recompose_polls
        self.workspace_recompose_polls = workspace_recompose_polls
        self.switcher_recompose_polls = switcher_recompose_polls
        self.members_recompose_polls = members_recompose_polls
        self.back_recompose_polls = back_recompose_polls
        self.switcher_hierarchy_calls = 0
        self.members_hierarchy_calls = 0
        self.home_hierarchy_calls = 0
        self.started = False
        self.semantic_clicks = []
        self.boundary_clicks = []
        self.back_count = 0

    def app_start(self, package_name):
        assert package_name == "notion.id"
        self.started = True
        self.state = "home"
        self.home_hierarchy_calls = 0

    def app_stop(self, package_name):
        assert package_name == "notion.id"

    def exists(self, selector):
        return self.count(selector) > 0

    def count(self, selector):
        if selector == {"packageName": "notion.id"}:
            return int(self.started)
        states = {
            "home": {
                (
                    ("className", "android.view.View"),
                    ("packageName", "notion.id"),
                    ("resourceId", "vision_tab_Home"),
                    ("selected", True),
                ),
                (
                    ("className", "android.view.View"),
                    ("clickable", True),
                    ("focusable", True),
                    ("packageName", "notion.id"),
                    ("resourceId", "vision_workspace_icon"),
                ),
            },
            "menu": {
                (
                    ("className", "android.view.View"),
                    ("clickable", True),
                    ("focusable", True),
                    ("packageName", "notion.id"),
                    ("resourceId", "home-tab.more.switch-workspace"),
                ),
                (
                    ("className", "android.view.View"),
                    ("clickable", True),
                    ("focusable", True),
                    ("packageName", "notion.id"),
                    ("resourceId", "home-tab.more.members"),
                ),
            },
            "switcher": {
                (
                    ("className", "android.widget.TextView"),
                    ("packageName", "notion.id"),
                    ("text", "Accounts"),
                ),
            },
            "members": {
                (
                    ("className", "android.widget.TextView"),
                    ("packageName", "notion.id"),
                    ("text", "Members"),
                ),
            },
        }
        return int(tuple(sorted(selector.items())) in states.get(self.state, set()))

    def click(self, selector):
        self.semantic_clicks.append(dict(selector))
        resource_id = selector.get("resourceId")
        if self.state == "home" and resource_id == "vision_workspace_icon":
            if self.home_hierarchy_calls <= max(
                self.home_recompose_polls,
                self.workspace_recompose_polls,
                self.back_recompose_polls,
            ):
                raise AssertionError(
                    "workspace menu opened before Home settled"
                )
            self.state = "menu"
            return
        if (
            self.state == "menu"
            and resource_id == "home-tab.more.switch-workspace"
        ):
            self.state = "switcher"
            self.switcher_hierarchy_calls = 0
            return
        if self.state == "menu" and resource_id == "home-tab.more.members":
            self.members_click_counts[self.selected] += 1
            self.state = (
                "home"
                if self.selected in self.members_noop_once_for
                and self.members_click_counts[self.selected] == 1
                else "members"
            )
            self.members_hierarchy_calls = 0
            return
        raise AssertionError(f"unexpected click: {self.state} {selector}")

    def click_bounds(self, bounds, *, anchor="center"):
        assert self.state == "switcher"
        assert anchor == "center"
        self.boundary_clicks.append(tuple(bounds))
        choices = {
            (40, 150, 1040, 250): "workspace-000001",
            (40, 250, 1040, 350): "workspace-000002",
        }
        self.selected = choices[tuple(bounds)]
        self.state = "home"
        if self.workspace_recompose_polls:
            self.home_hierarchy_calls = 0

    def long_click(self, selector):
        raise AssertionError("long click is not allowed")

    def long_click_bounds(self, bounds):
        raise AssertionError("long click is not allowed")

    def click_xpath(self, xpath):
        raise AssertionError("XPath click is not allowed")

    def back(self):
        self.back_count += 1
        if self.state in {"members", "switcher", "menu"}:
            self.state = "home"
            if self.back_recompose_polls:
                self.home_hierarchy_calls = 0
            return
        raise AssertionError(f"unexpected back from {self.state}")

    def swipe(self, direction, duration=0.2, distance_ratio=0.2):
        assert self.state == "home"
        assert direction in {"up", "down"}

    def hierarchy(self):
        if self.state == "home":
            self.home_hierarchy_calls += 1
            generation = min(
                self.home_hierarchy_calls,
                max(
                    self.home_recompose_polls,
                    self.workspace_recompose_polls,
                    self.back_recompose_polls,
                ),
            )
            return (
                '<hierarchy><node package="notion.id" '
                'class="android.view.View" resource-id="vision_tab_Home" '
                'selected="true" enabled="true" bounds="[200,80][460,224]"/>'
                '<node package="notion.id" class="android.view.View" '
                'resource-id="vision_workspace_icon" clickable="true" '
                'focusable="true" enabled="true" '
                'bounds="[48,80][192,224]"/>'
                f'<node package="notion.id" class="android.widget.TextView" '
                f'text="generation-{generation}" enabled="true" '
                'bounds="[200,250][600,310]"/></hierarchy>'
            )
        if self.state == "menu":
            return (
                '<hierarchy><node package="notion.id" '
                'class="android.view.View" '
                'resource-id="home-tab.more.switch-workspace" '
                'clickable="true" focusable="true" enabled="true" '
                'bounds="[72,212][804,346]"/>'
                '<node package="notion.id" class="android.view.View" '
                'resource-id="home-tab.more.members" clickable="true" '
                'focusable="true" enabled="true" '
                'bounds="[72,478][804,610]"/></hierarchy>'
            )
        if self.state == "switcher":
            self.switcher_hierarchy_calls += 1
            selected_one = (
                '<node package="notion.id" class="android.view.View" '
                'enabled="true" bounds="[920,165][1000,235]"/>'
                if self.selected == "workspace-000001"
                else ""
            )
            selected_two = (
                '<node package="notion.id" class="android.view.View" '
                'enabled="true" bounds="[920,265][1000,335]"/>'
                if self.selected == "workspace-000002"
                else ""
            )
            second_plan = (
                '<node package="notion.id" '
                'class="android.widget.TextView" text="Plus plan" '
                'enabled="true" bounds="[180,305][500,340]"/>'
                if self.switcher_hierarchy_calls
                > self.switcher_recompose_polls
                else ""
            )
            return (
                '<hierarchy><node package="notion.id" '
                'class="android.widget.TextView" text="Accounts" '
                'enabled="true" bounds="[400,40][680,100]"/>'
                '<node package="notion.id" class="android.widget.TextView" '
                'text="analyst@example.test" enabled="true" '
                'bounds="[40,100][800,145]"/>'
                '<node package="notion.id" class="android.view.View" '
                'clickable="true" focusable="true" enabled="true" '
                'bounds="[40,150][1040,250]">'
                '<node package="notion.id" class="android.view.View" '
                'enabled="true" bounds="[60,165][140,235]"/>'
                '<node package="notion.id" class="android.widget.TextView" '
                'text="Workspace One" enabled="true" '
                'bounds="[180,165][500,205]"/>'
                '<node package="notion.id" class="android.widget.TextView" '
                'text="Free plan" enabled="true" '
                'bounds="[180,205][500,240]"/>'
                f"{selected_one}</node>"
                '<node package="notion.id" class="android.view.View" '
                'clickable="true" focusable="true" enabled="true" '
                'bounds="[40,250][1040,350]">'
                '<node package="notion.id" class="android.view.View" '
                'enabled="true" bounds="[60,265][140,335]"/>'
                '<node package="notion.id" class="android.widget.TextView" '
                'text="Workspace Two" enabled="true" '
                'bounds="[180,265][500,305]"/>'
                f"{second_plan}{selected_two}</node>"
                '<node package="notion.id" class="android.view.View" '
                'resource-id="home-tab.account-switcher.logout" '
                'clickable="true" enabled="true" '
                'bounds="[40,600][1040,700]">'
                '<node package="notion.id" '
                'class="android.widget.TextView" text="Log out all" '
                'enabled="true" bounds="[180,625][500,675]"/>'
                '</node></hierarchy>'
            )
        if self.state == "members":
            self.members_hierarchy_calls += 1
            if self.members_hierarchy_calls <= self.members_recompose_polls:
                return (
                    '<hierarchy><node package="notion.id" '
                    'class="android.widget.TextView" text="Members" '
                    'enabled="true" bounds="[360,78][720,210]"/>'
                    '</hierarchy>'
                )
            if self.selected in self.offline_members_for:
                return ET.tostring(
                    offline_members_tree(), encoding="unicode"
                )
            number = "one" if self.selected == "workspace-000001" else "two"
            duplicate_link = (
                '<node package="notion.id" class="android.widget.EditText" '
                'text="https://app.notion.com/invite/duplicate-token" '
                'enabled="true" bounds="[80,225][1000,285]"/>'
                if self.selected in self.ambiguous_members_for
                else ""
            )
            return (
                '<hierarchy><node package="notion.id" '
                'class="android.widget.TextView" text="Members" '
                'enabled="true" bounds="[400,40][680,100]"/>'
                '<node package="notion.id" class="android.widget.EditText" '
                f'text="https://app.notion.com/invite/{number}-token" '
                'enabled="true" bounds="[80,160][1000,220]"/>'
                f"{duplicate_link}"
                '<node package="notion.id" class="android.view.View" '
                'enabled="true" bounds="[0,300][1080,460]">'
                '<node package="notion.id" class="android.widget.Image" '
                'content-desc="Analyst" enabled="true" '
                'bounds="[30,330][110,410]"/>'
                '<node package="notion.id" class="android.widget.TextView" '
                'text="Analyst (You)" enabled="true" '
                'bounds="[130,310][400,355]"/>'
                '<node package="notion.id" class="android.widget.TextView" '
                'text="Workspace owner" enabled="true" '
                'bounds="[410,310][650,355]"/>'
                '<node package="notion.id" class="android.widget.TextView" '
                'text="analyst@example.test" enabled="true" '
                'bounds="[130,360][600,420]"/>'
                '<node package="notion.id" class="android.widget.Button" '
                'text="Workspace owner" clickable="true" enabled="true" '
                'bounds="[660,330][1040,410]"/>'
                '</node></hierarchy>'
            )
        raise AssertionError(f"unexpected state: {self.state}")

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nnotion"

    def shell(self, command):
        if command == "getprop ro.product.model":
            return "Fake phone\n"
        if command == "getprop ro.build.version.release":
            return "16\n"
        raise AssertionError("shell is not part of this phase")

    def pull(self, remote_path, local_path):
        raise AssertionError("pull is not part of this phase")

    def window_size(self):
        return 1080, 2400


def test_repository_notion_profile_declares_exact_context_controls():
    try:
        profile = ProfileStore(ROOT / "profiles").load_app(
            "notion", "0.6.4030"
        )
    except ProfileError:
        profile = None

    assert profile is not None
    assert profile.routes == (Route.MATERIALIZE, Route.EXPORT)
    assert (
        profile.parameters["export_root"]
        == "/sdcard/Download/Notion"
    )
    assert set(profile.selectors) == {
        "notion.home",
        "notion.workspace-menu.open",
        "notion.workspace-menu.switch",
        "notion.workspace-menu.members",
        "notion.account-switcher",
        "notion.members",
    }


def test_parse_switcher_preserves_accounts_duplicates_and_initial_selection():
    collector = _collector()

    assert collector is not None
    inventory = collector._parse_switcher(switcher_tree())
    assert [item["workspace_ref"] for item in inventory["workspaces"]] == [
        "workspace-000001",
        "workspace-000002",
        "workspace-000003",
    ]
    assert inventory["initial_workspace_ref"] == "workspace-000002"
    assert inventory["workspaces"][0]["account_email"] == "first@example.test"
    assert inventory["workspaces"][2]["account_email"] == "second@example.test"
    assert inventory["workspaces"][2]["same_name_occurrence"] == 2


def test_parse_switcher_accepts_workspace_rows_without_plan_labels():
    collector = _collector()
    tree = switcher_tree()
    for row in tree.iter():
        for child in list(row):
            if child.get("text") in {"Free plan", "Plus plan"}:
                row.remove(child)

    assert collector is not None
    inventory = collector._parse_switcher(tree)
    assert [item["display_name"] for item in inventory["workspaces"]] == [
        "Shared",
        "Research",
        "Shared",
    ]
    assert [item["plan_label"] for item in inventory["workspaces"]] == [
        None,
        None,
        None,
    ]
    assert inventory["initial_workspace_ref"] == "workspace-000002"


def test_parse_members_keeps_displayed_identity_role_and_invite_link():
    collector = _collector()

    assert collector is not None
    assert collector._parse_members(members_tree()) == {
        "acquisition_status": "complete",
        "reason_code": None,
        "invite_link": "https://app.notion.com/invite/example-token",
        "members": [
            {
                "display_name": "Analyst (You)",
                "email": "analyst@example.test",
                "role": "Workspace owner",
                "is_current_user": True,
                "avatar_description": "Analyst",
            },
            {
                "display_name": "Reviewer",
                "email": "reviewer@example.test",
                "role": "Member",
                "is_current_user": False,
                "avatar_description": "Reviewer",
            },
        ],
    }


def test_parse_members_records_observed_online_requirement():
    collector = _collector()

    assert collector is not None
    assert collector._parse_members(offline_members_tree()) == {
        "acquisition_status": "unavailable",
        "reason_code": "notion_members_online_required",
        "invite_link": None,
        "members": [],
    }


def test_collects_every_workspace_members_and_restores_initial(tmp_path):
    notion = importlib.import_module("aura.apps.notion")
    collect = getattr(notion, "collect", None)

    assert callable(collect)
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
    system_ui = ProfileStore(ROOT / "profiles").load_system_ui("samsung")
    device = NotionDevice()
    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        system_ui,
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        collect,
        run_id="notion-context",
    )

    assert result.outcome.status.value == "complete"
    document = json.loads((result.run_dir / "acquisition.json").read_text())
    content = next(record for record in document["identifications"] if record["target_id"] == "notion.content")
    assert content["traversal_complete"] is True
    assert content["completion_condition"] == "all_workspaces_visited"
    assert result.outcome.details == {
        "account_count": 1,
        "workspace_count": 2,
        "workspace_selection_count": 2,
        "cross_workspace_transition_count": 1,
        "member_count": 2,
        "page_count": 0,
        "database_count": 0,
        "database_item_count": 0,
        "database_out_of_scope_count": 0,
        "attachment_count": 0,
        "attachment_materialized_count": 0,
        "attachment_out_of_scope_count": 0,
        "initial_workspace_restored": True,
    }
    assert {item.item_type for item in result.items} >= {
        "account",
        "workspace",
    }
    # Selectable workspace rows are not separate accounts: this switcher has
    # one email header and two workspace rows beneath that account.
    assert len([item for item in result.items if item.item_type == "account"]) == 1
    assert len([item for item in result.items if item.item_type == "workspace"]) == 2
    assert content["identified_item_count"] == 3
    assert all(
        attempt.acquisition_status.value == "acquired"
        for attempt in result.attempts
    )
    assert device.selected == "workspace-000001"
    assert device.state == "home"
    assert device.boundary_clicks == [
        (40, 150, 1040, 250),
        (40, 250, 1040, 350),
        (40, 150, 1040, 250),
    ]
    paths = {record.relative_path for record in result.artifacts}
    assert "artifacts/notion/account/account.json" in paths
    assert "artifacts/notion/account/workspaces.json" in paths
    assert "artifacts/notion/account/workspace-list.png" in paths
    assert "artifacts/notion/account/workspace-list.xml" in paths
    assert len([
        path
        for path in paths
        if path.endswith("/selection-verification.png")
    ]) == 2
    assert len([
        path
        for path in paths
        if path.endswith("/selection-verification.xml")
    ]) == 2
    assert not any(path.endswith("/workspace.json") for path in paths)
    assert not any(path.endswith("/workspace.png") for path in paths)
    assert not any(path.endswith("/workspace.xml") for path in paths)
    assert len([path for path in paths if path.endswith("/members.json")]) == 2
    assert len([path for path in paths if path.endswith("/pages.json")]) == 2
    summary = json.loads(
        (
            result.run_dir
            / "artifacts"
            / "notion"
            / "account"
            / "workspaces.json"
        ).read_text(encoding="utf-8")
    )
    assert summary["workspace_count"] == 2
    assert all(
        item["selection_verification_source"]["screen_path"].endswith(
            "/selection-verification.png"
        )
        and item["selection_verification_source"]["ui_tree_path"].endswith(
            "/selection-verification.xml"
        )
        and item["members_source"]["ui_tree_artifact_id"]
        and item["pages_source"]["path"].endswith("/pages.json")
        and item["page_count"] == 0
        and item["database_count"] == 0
        and item["database_item_count"] == 0
        and item["database_out_of_scope_count"] == 0
        and item["attachment_count"] == 0
        and item["attachment_materialized_count"] == 0
        and item["attachment_out_of_scope_count"] == 0
        for item in summary["workspaces"]
    )


def test_members_home_noop_retries_once(
    tmp_path,
):
    notion = importlib.import_module("aura.apps.notion")
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
    device = NotionDevice(
        offline_members_for={"workspace-000001", "workspace-000002"},
        members_noop_once_for={"workspace-000002"},
        members_recompose_polls=2,
        back_recompose_polls=2,
    )
    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-offline-members",
    )

    assert result.outcome.status.value == "partial"
    assert result.outcome.reason == "notion_members_online_required"
    assert result.outcome.details["workspace_count"] == 2
    assert result.outcome.details["members_unavailable_count"] == 2
    assert result.outcome.details["members_unavailable_workspace_refs"] == [
        "workspace-000001",
        "workspace-000002",
    ]
    assert result.outcome.details["initial_workspace_restored"] is True
    assert device.selected == "workspace-000001"
    assert device.members_click_counts == {
        "workspace-000001": 1,
        "workspace-000002": 2,
    }
    assert device.state == "home"
    paths = {item.relative_path for item in result.artifacts}
    assert len([path for path in paths if path.endswith("/members.json")]) == 2
    assert len([path for path in paths if path.endswith("/members.png")]) == 2
    assert len([path for path in paths if path.endswith("/members.xml")]) == 2
    assert len([path for path in paths if path.endswith("/pages.json")]) == 2
    member_records = [
        json.loads((result.run_dir / path).read_text(encoding="utf-8"))
        for path in paths
        if path.endswith("/members.json")
    ]
    assert all(
        record["acquisition_status"] == "unavailable"
        and record["reason_code"] == "notion_members_online_required"
        and record["invite_link"] is None
        and record["members"] == []
        for record in member_records
    )


def test_members_context_loss_reselects_target_workspace_once(
    tmp_path,
    monkeypatch,
):
    page_ids = {
        "workspace-000001": "11111111-1111-4111-8111-111111111111",
        "workspace-000002": "22222222-2222-4222-8222-222222222222",
    }

    class MembersContextLossDevice(NotionDevice):
        def click(self, selector):
            if (
                self.state == "menu"
                and selector.get("resourceId") == "home-tab.more.members"
                and self.selected == "workspace-000002"
                and self.members_click_counts[self.selected] == 0
            ):
                self.members_click_counts[self.selected] += 1
                self.selected = "workspace-000001"
                self.state = "home"
                return
            super().click(selector)

        def hierarchy(self):
            xml = super().hierarchy()
            if self.state != "home":
                return xml
            root = ET.fromstring(xml)
            ET.SubElement(
                root,
                "node",
                {
                    "package": "notion.id",
                    "class": "android.view.View",
                    "resource-id": (
                        "home-tab.private.page-row."
                        f"{page_ids[self.selected]}"
                    ),
                    "bounds": "[60,450][1020,575]",
                },
            )
            return ET.tostring(root, encoding="unicode")

    notion = importlib.import_module("aura.apps.notion")

    def collect_pages(context, workspace, prefix, target_ref):
        action = context.journal.record_action(
            f"pages_{workspace['workspace_ref']}", status="success"
        )
        return {
            "status": "complete",
            "reason_code": None,
            "source": {"path": f"{prefix}/pages.json"},
            "discovered_page_count": 0,
            "page_count": 0,
            "database_count": 0,
            "database_item_count": 0,
            "database_out_of_scope_count": 0,
            "attachment_count": 0,
            "attachment_materialized_count": 0,
            "attachment_out_of_scope_count": 0,
            "_action": action,
        }

    monkeypatch.setattr(
        notion.collector,
        "collect_workspace_pages",
        collect_pages,
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
    device = MembersContextLossDevice(
        offline_members_for={"workspace-000001", "workspace-000002"},
    )
    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-members-context-recovery",
    )

    assert result.outcome.reason == "notion_members_online_required"
    assert result.outcome.details["workspace_count"] == 2
    assert result.outcome.details["initial_workspace_restored"] is True
    assert device.members_click_counts == {
        "workspace-000001": 1,
        "workspace-000002": 2,
    }


def test_guest_workspace_without_members_control_collects_payload(
    tmp_path,
    monkeypatch,
):
    class GuestWorkspaceDevice(NotionDevice):
        def hierarchy(self):
            xml = super().hierarchy()
            if self.state != "menu" or self.selected != "workspace-000002":
                return xml
            root = ET.fromstring(xml)
            for node in list(root):
                if node.get("resource-id") == "home-tab.more.members":
                    root.remove(node)
            return ET.tostring(root, encoding="unicode")

    notion = importlib.import_module("aura.apps.notion")
    calls = []

    def collect_pages(context, workspace, prefix, target_ref):
        workspace_ref = str(workspace["workspace_ref"])
        calls.append(workspace_ref)
        action = context.journal.record_action(
            f"pages_{workspace_ref}", status="success"
        )
        return {
            "status": "complete",
            "reason_code": None,
            "source": {"path": f"{prefix}/pages.json"},
            "discovered_page_count": 0,
            "page_count": 0,
            "database_count": 0,
            "database_item_count": 0,
            "database_out_of_scope_count": 0,
            "attachment_count": 0,
            "attachment_materialized_count": 0,
            "attachment_out_of_scope_count": 0,
            "_action": action,
        }

    monkeypatch.setattr(
        notion.collector,
        "collect_workspace_pages",
        collect_pages,
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
    device = GuestWorkspaceDevice()
    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-guest-workspace",
    )

    assert result.outcome.reason == "notion_members_control_unavailable"
    assert result.outcome.details["workspace_count"] == 2
    assert result.outcome.details["initial_workspace_restored"] is True
    assert calls == ["workspace-000001", "workspace-000002"]
    assert device.members_click_counts == {"workspace-000001": 1}
    summary = json.loads(
        (
            result.run_dir
            / "artifacts"
            / "notion"
            / "account"
            / "workspaces.json"
        ).read_text(encoding="utf-8")
    )
    assert summary["workspaces"][1]["members_reason_code"] == (
        "notion_members_control_unavailable"
    )


def test_members_observation_waits_for_parseable_stable_tree(tmp_path):
    class TransientMembersDevice(NotionDevice):
        def hierarchy(self):
            if self.state != "members" or self.selected != "workspace-000002":
                return super().hierarchy()
            self.members_hierarchy_calls += 1
            if self.members_hierarchy_calls in {2, 3}:
                return (
                    '<hierarchy><node package="notion.id" '
                    'class="android.widget.TextView" text="Members" '
                    'enabled="true" bounds="[360,78][720,210]"/>'
                    '</hierarchy>'
                )
            return ET.tostring(members_tree(), encoding="unicode")

    notion = importlib.import_module("aura.apps.notion")
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
    result = AcquisitionRuntime(tmp_path, TransientMembersDevice()).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-transient-members",
    )

    assert result.outcome.status.value == "complete"
    assert result.outcome.reason is None
    assert result.outcome.details["workspace_count"] == 2
    assert result.outcome.details["initial_workspace_restored"] is True


def test_switcher_waits_for_workspace_menu_hierarchy(tmp_path):
    class DelayedMenuDevice(NotionDevice):
        def __init__(self):
            super().__init__()
            self.menu_hierarchy_calls = 0

        def click(self, selector):
            resource_id = selector.get("resourceId")
            if resource_id == "vision_workspace_icon":
                self.menu_hierarchy_calls = 0
            if (
                resource_id == "home-tab.more.switch-workspace"
                and self.menu_hierarchy_calls <= 2
            ):
                raise AssertionError("switcher opened before menu hierarchy")
            super().click(selector)

        def hierarchy(self):
            if self.state != "menu":
                return super().hierarchy()
            self.menu_hierarchy_calls += 1
            if self.menu_hierarchy_calls > 2:
                return super().hierarchy()
            self.state = "home"
            try:
                return super().hierarchy()
            finally:
                self.state = "menu"

    notion = importlib.import_module("aura.apps.notion")
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
    result = AcquisitionRuntime(tmp_path, DelayedMenuDevice()).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-delayed-menu",
    )

    assert result.outcome.status.value == "complete"


def test_workspace_payload_waits_for_changed_home_content(
    tmp_path,
    monkeypatch,
):
    page_ids = {
        "workspace-000001": "11111111-1111-4111-8111-111111111111",
        "workspace-000002": "22222222-2222-4222-8222-222222222222",
    }

    class DelayedWorkspaceContentDevice(NotionDevice):
        def __init__(self):
            super().__init__()
            self.content_workspace = "workspace-000001"
            self.pending_workspace = None
            self.content_hierarchy_calls = 0
            self.transition_interrupted = False

        def click(self, selector):
            if (
                selector.get("resourceId") == "vision_workspace_icon"
                and self.pending_workspace is not None
            ):
                self.transition_interrupted = True
            super().click(selector)

        def click_bounds(self, bounds, *, anchor="center"):
            previous = self.selected
            super().click_bounds(bounds, anchor=anchor)
            if self.selected != previous:
                self.pending_workspace = self.selected
                self.content_hierarchy_calls = 0
                self.transition_interrupted = False

        def hierarchy(self):
            xml = super().hierarchy()
            if self.state != "home":
                return xml
            if (
                self.pending_workspace is not None
                and not self.transition_interrupted
            ):
                self.content_hierarchy_calls += 1
                if self.content_hierarchy_calls > 4:
                    self.content_workspace = self.pending_workspace
                    self.pending_workspace = None
            root = ET.fromstring(xml)
            ET.SubElement(
                root,
                "node",
                {
                    "package": "notion.id",
                    "class": "android.view.View",
                    "resource-id": (
                        "home-tab.private.page-row."
                        f"{page_ids[self.content_workspace]}"
                    ),
                    "bounds": "[60,450][1020,575]",
                },
            )
            return ET.tostring(root, encoding="unicode")

    notion = importlib.import_module("aura.apps.notion")
    calls = []

    def collect_pages(context, workspace, prefix, target_ref):
        workspace_ref = str(workspace["workspace_ref"])
        assert context.device.content_workspace == workspace_ref
        calls.append(workspace_ref)
        action = context.journal.record_action(
            f"pages_{workspace_ref}", status="success"
        )
        return {
            "status": "complete",
            "reason_code": None,
            "source": {"path": f"{prefix}/pages.json"},
            "discovered_page_count": 0,
            "page_count": 0,
            "database_count": 0,
            "database_item_count": 0,
            "database_out_of_scope_count": 0,
            "attachment_count": 0,
            "attachment_materialized_count": 0,
            "attachment_out_of_scope_count": 0,
            "_action": action,
        }

    monkeypatch.setattr(
        notion.collector,
        "collect_workspace_pages",
        collect_pages,
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
    result = AcquisitionRuntime(
        tmp_path, DelayedWorkspaceContentDevice()
    ).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-delayed-workspace-content",
    )

    assert result.outcome.status.value == "complete"
    assert calls == ["workspace-000001", "workspace-000002"]


def test_notion_fails_canonical_checkpoint_without_stopping_app(tmp_path):
    class ResumedModalDevice(NotionDevice):
        def __init__(self):
            super().__init__()
            self.state = "export_dialog"
            self.start_calls = 0
            self.stop_calls = 0

        def app_start(self, package_name):
            assert package_name == "notion.id"
            self.start_calls += 1
            self.started = True
            self.state = "export_dialog"

        def app_stop(self, package_name):
            assert package_name == "notion.id"
            self.stop_calls += 1

        def hierarchy(self):
            if self.state == "export_dialog":
                return (
                    '<hierarchy><node package="notion.id" '
                    'class="android.widget.TextView" text="Export format" '
                    'enabled="true" bounds="[40,300][600,380]"/>'
                    '</hierarchy>'
                )
            return super().hierarchy()

    notion = importlib.import_module("aura.apps.notion")
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
    device = ResumedModalDevice()

    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-resumed-modal",
    )

    assert result.outcome.status.value == "failed"
    assert result.outcome.reason == "notion_home_unavailable"
    assert device.start_calls == 1
    assert device.stop_calls == 0


def test_collect_preserves_attachment_counts_for_complete_and_partial(
    monkeypatch,
):
    notion = importlib.import_module("aura.apps.notion")
    context = SimpleNamespace(
        app_profile=ProfileStore(ROOT / "profiles").load_app("notion", "0.6.4030"),
        base_context={
            "route": "materialize",
            "condition": {
                "target": {
                    "kind": "account",
                    "ref": "account.notion.test.001",
                },
            },
        },
        start_app=lambda: True,
        outcomes=(),
        complete_open_attempts=lambda *args: None,
        interrupt_open_attempts=lambda *args: None,
    )
    counts = {
        "attachment_count": 6,
        "attachment_materialized_count": 5,
        "attachment_out_of_scope_count": 1,
    }
    for status in ("complete", "partial"):
        monkeypatch.setattr(
            notion.collector,
            "collect",
            lambda *args, status=status: {
                "status": status,
                "reason_code": (
                    None
                    if status == "complete"
                    else "notion_attachment_materialize_partial"
                ),
                **counts,
            },
        )
        assert notion.collect(context).details == counts


def test_entrypoint_passes_export_route_and_returns_export_counts(
    monkeypatch,
):
    notion = importlib.import_module("aura.apps.notion")
    calls = []
    context = SimpleNamespace(
        app_profile=ProfileStore(ROOT / "profiles").load_app("notion", "0.6.4030"),
        base_context={
            "route": "export",
            "condition": {
                "target": {
                    "kind": "account",
                    "ref": "account.notion.test.001",
                },
            },
        },
        start_app=lambda: True,
        outcomes=(),
        complete_open_attempts=lambda *args: None,
        interrupt_open_attempts=lambda *args: None,
    )

    def collect_report(_context, target_ref, route):
        calls.append((target_ref, route))
        return {
            "status": "complete",
            "reason_code": None,
            "discovered_top_level_count": 2,
            "export_count": 2,
            "general_page_export_count": 1,
            "database_export_count": 1,
            "include_subpages_count": 2,
        }

    monkeypatch.setattr(notion.collector, "collect", collect_report)

    outcome = notion.collect(context)

    assert calls == [
        ("account.notion.test.001", Route.EXPORT),
    ]
    assert outcome.status.value == "complete"
    assert outcome.details == {
        "discovered_top_level_count": 2,
        "export_count": 2,
        "general_page_export_count": 1,
        "database_export_count": 1,
        "include_subpages_count": 2,
    }


def test_export_route_dispatches_each_workspace_to_native_export(
    tmp_path,
    monkeypatch,
):
    notion = importlib.import_module("aura.apps.notion")
    calls = []

    def collect_exports(context, workspace, prefix, target_ref):
        calls.append(workspace["workspace_ref"])
        action = context.journal.record_action(
            f"export_{workspace['workspace_ref']}",
            status="success",
        )
        return {
            "status": "complete",
            "source": {"path": f"{prefix}/exports/exports.json"},
            "discovered_top_level_count": 2,
            "export_count": 2,
            "general_page_export_count": 1,
            "database_export_count": 1,
            "include_subpages_count": 2,
            "_action": action,
        }

    monkeypatch.setattr(
        notion.collector,
        "collect_workspace_exports",
        collect_exports,
        raising=False,
    )
    monkeypatch.setattr(
        notion.collector,
        "collect_workspace_pages",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("materialize collector must not run")
        ),
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

    result = AcquisitionRuntime(tmp_path, NotionDevice()).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.EXPORT,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-export-route",
    )

    assert result.outcome.status.value == "complete"
    assert calls == ["workspace-000001", "workspace-000002"]
    assert result.outcome.details["export_count"] == 4
    assert result.outcome.details["general_page_export_count"] == 2
    assert result.outcome.details["database_export_count"] == 2
    assert result.outcome.details["include_subpages_count"] == 4
    summary = json.loads(
        (
            result.run_dir
            / "artifacts"
            / "notion"
            / "account"
            / "workspaces.json"
        ).read_text(encoding="utf-8")
    )
    assert all(
        item["exports_source"]["path"].endswith(
            "/exports/exports.json"
        )
        for item in summary["workspaces"]
    )


def test_materialize_partial_workspace_does_not_abort_remaining_workspaces(
    tmp_path,
    monkeypatch,
):
    notion = importlib.import_module("aura.apps.notion")
    calls = []

    def collect_pages(context, workspace, prefix, target_ref):
        workspace_ref = workspace["workspace_ref"]
        calls.append(workspace_ref)
        partial = workspace_ref == "workspace-000001"
        action = context.journal.record_action(
            f"pages_{workspace_ref}", status="success"
        )
        return {
            "status": "partial" if partial else "complete",
            "reason_code": (
                "notion_attachment_materialize_partial"
                if partial
                else None
            ),
            "source": {"path": f"{prefix}/pages.json"},
            "discovered_page_count": 1,
            "page_count": 0 if partial else 1,
            "database_count": 0,
            "database_item_count": 0,
            "database_out_of_scope_count": 0,
            "attachment_count": 1,
            "attachment_materialized_count": 0 if partial else 1,
            "attachment_out_of_scope_count": 0,
            "_action": action,
        }

    monkeypatch.setattr(
        notion.collector,
        "collect_workspace_pages",
        collect_pages,
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

    result = AcquisitionRuntime(tmp_path, NotionDevice()).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-materialize-partial-workspace",
    )

    assert calls == ["workspace-000001", "workspace-000002"]
    assert result.outcome.status.value == "partial"
    assert result.outcome.reason == (
        "notion_attachment_materialize_partial"
    )
    assert result.outcome.details["workspace_count"] == 2
    assert result.outcome.details["page_count"] == 1
    assert result.outcome.details["attachment_count"] == 2
    assert result.outcome.details["attachment_materialized_count"] == 1


def test_members_failure_records_workspace_and_restores_initial_home(tmp_path):
    notion = importlib.import_module("aura.apps.notion")
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
    system_ui = ProfileStore(ROOT / "profiles").load_system_ui("samsung")
    device = NotionDevice(
        ambiguous_members_for={"workspace-000002"}
    )
    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        system_ui,
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-context-partial",
    )

    assert result.outcome.status.value == "partial"
    assert result.outcome.reason == "notion_members_context_ambiguous"
    assert result.outcome.details["failed_workspace_ref"] == (
        "workspace-000002"
    )
    assert result.outcome.details["initial_workspace_restored"] is True
    assert device.selected == "workspace-000001"
    assert device.state == "home"
    account = result.run_dir / "artifacts/notion/account"
    assert (account / "account.json").is_file()
    summary = json.loads(
        (account / "workspaces.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "partial"
    assert summary["reason_code"] == "notion_members_context_ambiguous"
    assert summary["discovered_workspace_count"] == 2
    assert summary["discovered_workspace_refs"] == [
        "workspace-000001",
        "workspace-000002",
    ]
    assert summary["workspace_count"] == 1
    assert summary["failed_workspace_ref"] == "workspace-000002"
    assert [item["workspace_ref"] for item in summary["workspaces"]] == [
        "workspace-000001"
    ]


def test_restore_verifies_device_after_post_selection_failure(tmp_path):
    class PostSelectionFailureDevice(NotionDevice):
        def __init__(self):
            super().__init__()
            self.fail_next_hierarchy = False

        def click_bounds(self, bounds, *, anchor="center"):
            super().click_bounds(bounds, anchor=anchor)
            if self.selected == "workspace-000002":
                self.fail_next_hierarchy = True

        def hierarchy(self):
            if self.fail_next_hierarchy:
                self.fail_next_hierarchy = False
                return "<invalid"
            return super().hierarchy()

    notion = importlib.import_module("aura.apps.notion")
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
    device = PostSelectionFailureDevice()

    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-post-selection-failure",
    )

    assert result.outcome.status.value == "partial"
    assert result.outcome.details["initial_workspace_restored"] is True
    assert device.selected == "workspace-000001"
    assert device.state == "home"


def test_waits_for_stable_home_before_opening_workspace_menu(tmp_path):
    notion = importlib.import_module("aura.apps.notion")
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
    device = NotionDevice(home_recompose_polls=2)
    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-context-home-recompose",
    )

    assert result.outcome.status.value == "complete"
    workspace_menu_clicks = [
        item
        for item in device.semantic_clicks
        if item.get("resourceId") == "vision_workspace_icon"
    ]
    assert len(workspace_menu_clicks) == 8


def test_waits_for_workspace_home_recomposition_before_next_action(tmp_path):
    notion = importlib.import_module("aura.apps.notion")
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
    device = NotionDevice(workspace_recompose_polls=2)
    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-workspace-recompose",
    )

    assert result.outcome.status.value == "complete"
    assert result.outcome.details["initial_workspace_restored"] is True


def test_waits_for_complete_workspace_rows_in_expanding_switcher(tmp_path):
    notion = importlib.import_module("aura.apps.notion")
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
    device = NotionDevice(switcher_recompose_polls=2)
    result = AcquisitionRuntime(tmp_path, device).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-switcher-recompose",
    )

    assert result.outcome.status.value == "complete"
    assert result.outcome.details["workspace_count"] == 2


def test_retains_last_switcher_tree_and_parser_error_on_inventory_failure(
    tmp_path,
):
    class MissingWorkspaceDevice(NotionDevice):
        def hierarchy(self):
            if self.state != "switcher":
                return super().hierarchy()
            return (
                '<hierarchy><node package="notion.id" '
                'class="android.widget.TextView" text="Accounts" '
                'enabled="true" bounds="[400,40][680,100]"/>'
                '<node package="notion.id" class="android.widget.TextView" '
                'text="analyst@example.test" enabled="true" '
                'bounds="[40,100][800,145]"/>'
                '<node package="notion.id" class="android.view.View" '
                'resource-id="home-tab.account-switcher.addAccount" '
                'clickable="true" enabled="true" '
                'bounds="[40,720][1040,820]">'
                '<node package="notion.id" '
                'class="android.widget.TextView" text="Add an account" '
                'enabled="true" bounds="[180,745][500,795]"/>'
                '</node></hierarchy>'
            )

    notion = importlib.import_module("aura.apps.notion")
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
    result = AcquisitionRuntime(tmp_path, MissingWorkspaceDevice()).run(
        profile,
        ProfileStore(ROOT / "profiles").load_system_ui("samsung"),
        Route.MATERIALIZE,
        {
            "target": {
                "kind": "account",
                "ref": "account.notion.test.001",
            },
        },
        notion.collect,
        run_id="notion-missing-workspace",
    )

    assert result.outcome.status.value == "failed"
    assert result.outcome.reason == "notion_workspace_inventory_unavailable"
    assert result.outcome.details["collector_error"] == (
        "workspace inventory is unavailable"
    )
    diagnostic = next(
        item
        for item in result.artifacts
        if item.relative_path.endswith("workspace-switcher-000001-last.xml")
    )
    assert diagnostic.context["parser_error"] == (
        "workspace inventory is unavailable"
    )
    assert "Accounts" in (
        result.run_dir / diagnostic.relative_path
    ).read_text(encoding="utf-8")
    observation = next(
        item
        for item in result.observations
        if item.observation_id == diagnostic.observation_id
    )
    outcome = next(
        item
        for item in result.outcomes
        if item.attempt_id == diagnostic.attempt_id
    )
    assert observation.attempt_id == diagnostic.attempt_id
    assert outcome.reason == "workspace inventory is unavailable"
