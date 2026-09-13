from pathlib import Path
from types import SimpleNamespace

from aura import cli
from aura.apps.chrome import collector as chrome_collector
from aura.apps.chrome.collector import (
    _bookmark_rows,
    merge_windows,
    parse_bookmarks,
    parse_account,
    parse_downloads,
    parse_history,
    parse_recent_tabs,
    wait_for_bookmark_title,
)
from aura.profiles import ProfileStore
from aura.ui import UiTimeout
from aura.artifacts import ArtifactStore
from aura.journal import EventJournal
from aura.runtime import AcquisitionRuntime, RunContext
from aura.models import Outcome, OutcomeStatus, Route
import json
from fakes import FakeDevice


def test_download_windows_preserve_ten_source_occurrences_and_enrich_edges():
    records = []
    windows = sorted((Path(__file__).parent / "fixtures/chrome-downloads").glob("*.xml"))
    assert len(windows) == 4
    for path in windows:
        visible = parse_downloads(path.read_text())
        records = chrome_collector.merge_download_windows(records, visible)
    assert [r["title"] for r in records] == [
        "AURA-EVAL-08-audio.wav", "AURA-EVAL-07-image.png",
        "AURA-EVAL-06-contact.vcf", "AURA-EVAL-05-events.log.txt",
        "AURA-EVAL-04-notes.md", "AURA-EVAL-03-metadata.json",
        "AURA-EVAL-02-timeline.csv", "AURA-EVAL-01-overview.txt",
        "FiddlerRoot.cer (1).crt", "FiddlerRoot.cer.crt",
    ]
    assert [r["date_group"] for r in records] == ["Today - 1 Sept 2026"] * 8 + ["22 Nov 2025"] * 2
    assert records[1]["caption"] == ""
    assert records[6]["caption"] == "212 B • 127.0.0.1:8765"
    assert records[8]["caption"] == "0.95 kB • ipv4.fiddler:8888"
    assert chrome_collector.merge_download_windows(records, visible) == records


def test_download_overlap_preserves_separate_equal_occurrences_and_dates():
    def row(title, date="Today", caption="1 B"):
        return {"title": title, "date_group": date, "caption": caption}
    first = [row("same"), row("same"), row("anchor")]
    second = [row("anchor", ""), row("same", ""), row("same", "Yesterday")]
    assert chrome_collector.merge_download_windows(first, second) == [
        *first, row("same"), row("same", "Yesterday"),
    ]
    assert chrome_collector.merge_download_windows(first, [row("unseen", "")])[-1] == row("unseen", "")


def test_download_collection_stops_at_repeated_window_with_ten_bound_records(tmp_path, monkeypatch):
    windows = [path.read_text() for path in sorted((Path(__file__).parent / "fixtures/chrome-downloads").glob("*.xml"))]
    class WindowsDevice(FakeDevice):
        index = 0
        def hierarchy(self):
            return windows[self.index]
        def swipe(self, direction, duration=0.2):
            self.index = min(self.index + 1, len(windows) - 1)
    monkeypatch.setattr(chrome_collector, "_open_surface", lambda context, *args: context.journal.record_action("open_downloads", status="success"))
    monkeypatch.setattr(chrome_collector.time, "sleep", lambda seconds: None)
    profiles = ProfileStore(Path(__file__).resolve().parents[1] / "profiles")
    def collect(context):
        count, action = chrome_collector._collect_list(context, surface="downloads", target_id="chrome.downloads", item_type="download_collection", menu_item="downloads", marker="downloads", parser=parse_downloads)
        assert count == 10
        return Outcome(OutcomeStatus.COMPLETE, action=action)
    result = AcquisitionRuntime(tmp_path, WindowsDevice()).run(
        profiles.load_app("chrome", "150.0.7871.124"), profiles.load_system_ui("samsung"),
        Route.MATERIALIZE, {"acquisition_environment": "device_only"}, collect, run_id="chrome-windows",
    )
    document = json.loads((result.run_dir / "acquisition.json").read_text())
    target = next(record for record in document["identifications"] if record["target_id"] == "chrome.downloads")
    assert target["identified_item_count"] == 10
    assert target["completion_condition"] == "repeated_hierarchy"
    assert target["traversal_complete"] is True
    records = json.loads((result.run_dir / "artifacts/chrome/downloads.json").read_text())["records"]
    assert len(records) == 10
    assert len(list((result.run_dir / "artifacts/chrome/downloads").glob("*.xml"))) == 4


def test_merge_windows_removes_scroll_overlap_but_keeps_real_duplicates():
    first = [{"title": "A"}, {"title": "A"}, {"title": "B"}]
    second = [{"title": "B"}, {"title": "C"}]

    assert merge_windows(first, second) == [
        {"title": "A"},
        {"title": "A"},
        {"title": "B"},
        {"title": "C"},
    ]


def test_wait_for_bookmark_title_ignores_transient_hierarchy_changes():
    values = iter(
        [
            '<hierarchy><node resource-id="com.android.chrome:id/action_bar"><node class="android.widget.TextView" text="Bookmarks"/></node><node selected="true"/></hierarchy>',
            '<hierarchy><node resource-id="com.android.chrome:id/action_bar"><node class="android.widget.TextView" text="Folder"/></node></hierarchy>',
        ]
    )
    context = SimpleNamespace(
        device=SimpleNamespace(hierarchy=lambda: next(values)),
        ui=SimpleNamespace(wait_until=lambda predicate: predicate() or predicate()),
    )

    wait_for_bookmark_title(context, "Bookmarks")


def test_return_to_bookmark_root_backs_out_of_remembered_folder():
    folder = '<hierarchy><node resource-id="com.android.chrome:id/action_bar"><node class="android.widget.TextView" text="Mobile bookmarks"/></node></hierarchy>'
    root = '<hierarchy><node resource-id="com.android.chrome:id/action_bar"><node class="android.widget.TextView" text="Bookmarks"/></node><node resource-id="com.android.chrome:id/selectable_list_recycler_view"><node><node resource-id="com.android.chrome:id/title" text="In your Google Account"/></node></node></hierarchy>'
    state = {"hierarchy": folder}
    backs = []

    def click(*args, **kwargs):
        backs.append((args, kwargs))
        state["hierarchy"] = root
        return "back-action"

    context = SimpleNamespace(
        device=SimpleNamespace(hierarchy=lambda: state["hierarchy"]),
        app_profile=SimpleNamespace(
            selectors={
                "chrome.bookmarks.go-back": {
                    "selector": {
                        "kind": "content_description",
                        "owner_package": "com.android.chrome",
                        "value": "Go back",
                    }
                }
            }
        ),
        ui=SimpleNamespace(click=click, wait_until=lambda predicate: predicate()),
    )

    assert chrome_collector._return_to_bookmark_root(context, "open-action") == "back-action"
    assert len(backs) == 1


def test_parse_history_keeps_date_group_and_ignores_controls():
    xml = """<hierarchy>
      <node class="android.widget.TextView" text="Today" />
      <node class="android.widget.FrameLayout" clickable="true">
        <node resource-id="com.android.chrome:id/title" text="Example" />
        <node resource-id="com.android.chrome:id/description" text="example.test" />
        <node resource-id="com.android.chrome:id/end_button" content-desc="Remove" />
      </node>
      <node resource-id="com.android.chrome:id/clear_browsing_data_button" text="Clear browsing data" />
    </hierarchy>"""

    assert parse_history(xml) == [
        {"date_group": "Today", "title": "Example", "displayed_address": "example.test"}
    ]


def test_parse_downloads_keeps_date_group_and_visible_metadata():
    xml = """<hierarchy>
      <node resource-id="com.android.chrome:id/date" text="August 31" />
      <node class="android.view.ViewGroup" clickable="true">
        <node resource-id="com.android.chrome:id/title" text="report.pdf" />
        <node resource-id="com.android.chrome:id/caption" text="1.2 MB · example.test" />
        <node resource-id="com.android.chrome:id/more" content-desc="More options" />
      </node>
    </hierarchy>"""

    assert parse_downloads(xml) == [
        {"date_group": "August 31", "title": "report.pdf", "caption": "1.2 MB · example.test"}
    ]


def test_parse_bookmarks_distinguishes_folders_and_bookmarks():
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" clickable="true" content-desc="Mobile bookmarks">
        <node resource-id="com.android.chrome:id/folder_view" />
        <node resource-id="com.android.chrome:id/child_count_text" text="2" />
        <node resource-id="com.android.chrome:id/title" text="Mobile bookmarks" />
        <node resource-id="com.android.chrome:id/local_bookmark_image" content-desc="Preview" />
      </node>
      <node class="android.widget.FrameLayout" clickable="true" content-desc="Example bookmark">
        <node resource-id="com.android.chrome:id/title" text="Example" />
        <node resource-id="com.android.chrome:id/local_bookmark_image" content-desc="Preview" />
      </node>
    </hierarchy>"""

    assert parse_bookmarks(xml, folder_path=("Bookmarks",)) == [
        {
            "kind": "folder",
            "folder_path": ["Bookmarks"],
            "title": "Mobile bookmarks",
            "child_count": "2",
        },
        {
            "kind": "bookmark",
            "folder_path": ["Bookmarks"],
            "title": "Example",
        },
    ]


def test_parse_bookmarks_keeps_sections_for_duplicate_root_folder_names():
    xml = """<hierarchy>
      <node resource-id="com.android.chrome:id/selectable_list_recycler_view">
        <node class="android.widget.LinearLayout">
          <node resource-id="com.android.chrome:id/title" text="In your Google Account" />
        </node>
        <node class="android.widget.FrameLayout" clickable="true">
          <node resource-id="com.android.chrome:id/child_count_text" text="1" />
          <node resource-id="com.android.chrome:id/title" text="Mobile bookmarks" />
        </node>
        <node class="android.widget.LinearLayout">
          <node resource-id="com.android.chrome:id/title" text="Only on this device" />
        </node>
        <node class="android.widget.FrameLayout" clickable="true">
          <node resource-id="com.android.chrome:id/child_count_text" text="7" />
          <node resource-id="com.android.chrome:id/title" text="Mobile bookmarks" />
          <node resource-id="com.android.chrome:id/local_bookmark_image" content-desc="Not synced" />
        </node>
      </node>
    </hierarchy>"""

    records = parse_bookmarks(xml, folder_path=())

    assert [record["folder_path"] for record in records] == [
        ["In your Google Account"],
        ["Only on this device"],
    ]
    assert [record["kind"] for record in records] == ["folder", "folder"]


def test_bookmark_rows_use_accessibility_description_instead_of_bounds():
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" clickable="true"
            content-desc="Mobile bookmarks 1 bookmark" bounds="[0,1000][1080,1282]">
        <node resource-id="com.android.chrome:id/child_count_text" text="1" />
        <node resource-id="com.android.chrome:id/title" text="Mobile bookmarks" />
      </node>
    </hierarchy>"""

    assert _bookmark_rows(xml, ()) == [
        (
            {
                "kind": "folder",
                "folder_path": [],
                "title": "Mobile bookmarks",
                "child_count": "1",
            },
            {
                "description": "Mobile bookmarks 1 bookmark",
                "packageName": "com.android.chrome",
            },
        )
    ]


def test_bookmark_rows_allow_bookmarks_without_accessibility_description():
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" clickable="true">
        <node resource-id="com.android.chrome:id/title" text="Example" />
        <node resource-id="com.android.chrome:id/local_bookmark_image" content-desc="Preview" />
      </node>
    </hierarchy>"""

    assert _bookmark_rows(xml, ("Mobile bookmarks",)) == [
        (
            {
                "kind": "bookmark",
                "folder_path": ["Mobile bookmarks"],
                "title": "Example",
            },
            None,
        )
    ]


def test_parse_recent_tabs_keeps_group_for_each_tab():
    xml = """<hierarchy>
      <node resource-id="com.android.chrome:id/recent_tabs_group_view">
        <node resource-id="com.android.chrome:id/device_label" text="Recently closed" />
      </node>
      <node resource-id="com.android.chrome:id/recent_tabs_list_item_layout">
        <node resource-id="com.android.chrome:id/title_row" text="Example" />
        <node resource-id="com.android.chrome:id/domain_row" text="example.test" />
      </node>
    </hierarchy>"""

    assert parse_recent_tabs(xml) == [
        {"group": "Recently closed", "title": "Example", "domain": "example.test"}
    ]


def test_parse_account_keeps_only_visible_account_identity():
    xml = """<hierarchy>
      <node resource-id="com.android.chrome:id/account_management_account_row">
        <node resource-id="android:id/title" text="Example User" />
        <node resource-id="android:id/summary" text="user@example.test" />
      </node>
    </hierarchy>"""

    assert parse_account(xml) == {
        "display_name": "Example User",
        "email": "user@example.test",
    }


def test_collect_account_observes_once_without_scrolling(monkeypatch, tmp_path):
    xml = """<hierarchy>
      <node resource-id="com.android.chrome:id/account_management_account_row">
        <node resource-id="android:id/title" text="Example User" />
        <node resource-id="android:id/summary" text="user@example.test" />
      </node>
    </hierarchy>"""
    swipes = []
    profiles = ProfileStore(Path(__file__).resolve().parents[1] / "profiles")
    journal = EventJournal(tmp_path / "events.jsonl")
    context = RunContext(run_dir=tmp_path, base_context={}, device=FakeDevice(hierarchy_before=xml), journal=journal,
                         artifacts=ArtifactStore(tmp_path, journal), ui=SimpleNamespace(swipe=lambda *args, **kwargs: swipes.append(args)),
                         app_profile=profiles.load_app("chrome", "150.0.7871.124"), system_ui_profile=profiles.load_system_ui("samsung"))
    monkeypatch.setattr(chrome_collector, "_open_surface", lambda *args: journal.record_action("open", status="success"))

    count, _ = chrome_collector._collect_list(
        context,
        surface="account",
        target_id="chrome.account",
        item_type="account_context",
        menu_item="chrome.menu.settings",
        marker="chrome.screen.settings",
        parser=chrome_collector._parse_account_list,
        scroll=False,
    )

    assert count == 1
    assert swipes == []
    assert context.identifications[0].completion_condition == "single_screen_parsed"


def test_open_surface_retries_menu_after_transient_timeout():
    clicks = []

    def click(name, selector, **kwargs):
        clicks.append(name)
        if name == "open_chrome_menu":
            raise UiTimeout("menu transition was not observed")
        return name

    selectors = {
        name: {
            "selector": {
                "kind": "resource_id",
                "owner_package": "com.android.chrome",
                "value": f"com.android.chrome:id/{name.replace('.', '_')}",
            }
        }
        for name in (
            "chrome.close",
            "chrome.home",
            "chrome.menu",
            "chrome.menu.list",
            "chrome.menu.settings",
            "chrome.screen.settings",
        )
    }
    context = SimpleNamespace(
        device=SimpleNamespace(exists=lambda selector: False),
        ui=SimpleNamespace(click=click),
        app_profile=SimpleNamespace(selectors=selectors, timings={}),
    )

    action = chrome_collector._open_surface(
        context, "chrome.menu.settings", "chrome.screen.settings"
    )

    assert action == "open_chrome_menu_settings"
    assert clicks == [
        "open_chrome_menu",
        "open_chrome_menu_retry",
        "open_chrome_menu_settings",
    ]


def test_chrome_is_registered_with_browser_condition():
    assert cli.APP_PACKAGES["chrome"] == "com.android.chrome"
    assert cli.COLLECTORS["chrome"] is not None
    assert cli._condition("chrome", cli.Route.MATERIALIZE, "device_only")[
        "target"
    ] == {"kind": "browser", "ref": "browser.chrome.all"}


def test_chrome_profile_declares_device_only_account_and_browser_collections():
    root = Path(__file__).resolve().parents[1] / "profiles"
    profile = ProfileStore(root).load_app("chrome", "150.0.7871.124")

    assert [target.target_id for target in profile.targets] == [
        "chrome.account",
        "chrome.history",
        "chrome.downloads",
        "chrome.bookmarks",
        "chrome.recent_tabs",
    ]
    assert all(target.acquisition_environments == ("device_only",) for target in profile.targets)
