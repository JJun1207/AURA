from pathlib import Path
from types import SimpleNamespace

from aura import cli
from aura.apps.samsung_browser.collector import (
    _return_to_main,
    dedupe_history,
    parse_bookmarks,
    parse_downloads,
    parse_history,
    parse_saved_pages,
)
from aura.profiles import ProfileStore


def test_parse_history_keeps_visible_title_address_and_time():
    xml = """<hierarchy>
      <node resource-id="com.sec.android.app.sbrowser:id/title" text="Today" />
      <node resource-id="com.sec.android.app.sbrowser:id/history_relative" clickable="true">
        <node resource-id="com.sec.android.app.sbrowser:id/history_title" text="Example" />
        <node resource-id="com.sec.android.app.sbrowser:id/history_url" text="example.test" />
        <node resource-id="com.sec.android.app.sbrowser:id/history_time" text="10:30 AM" />
      </node>
    </hierarchy>"""

    assert parse_history(xml) == [
        {
            "date_group": "Today",
            "title": "Example",
            "displayed_address": "example.test",
            "displayed_time": "10:30 AM",
        }
    ]


def test_dedupe_history_merges_repeated_rendering_without_losing_date():
    first = {
        "date_group": "Today",
        "title": "Example",
        "displayed_address": "example.test",
        "displayed_time": "10:30 AM",
    }
    repeated = {**first, "date_group": ""}

    assert dedupe_history([first, repeated]) == [first]


def test_parse_downloads_keeps_visible_file_metadata():
    xml = """<hierarchy>
      <node resource-id="com.sec.android.app.sbrowser:id/download_item_parent" clickable="true">
        <node resource-id="com.sec.android.app.sbrowser:id/download_item_title" text="report.pdf" />
        <node resource-id="com.sec.android.app.sbrowser:id/download_item_url" text="example.test" />
        <node resource-id="com.sec.android.app.sbrowser:id/download_item_total_size" text="1.2 MB" />
      </node>
    </hierarchy>"""

    assert parse_downloads(xml) == [
        {"title": "report.pdf", "source": "example.test", "displayed_size": "1.2 MB"}
    ]


def test_parse_saved_pages_keeps_visible_page_metadata():
    xml = """<hierarchy>
      <node resource-id="com.sec.android.app.sbrowser:id/saved_page_list_view_layout" clickable="true">
        <node resource-id="com.sec.android.app.sbrowser:id/saved_page_list_view_title_text_view" text="Example" />
        <node resource-id="com.sec.android.app.sbrowser:id/saved_page_list_view_url_text_view" text="example.test" />
        <node resource-id="com.sec.android.app.sbrowser:id/saved_page_list_view_description_text_view" text="Saved page description" />
      </node>
    </hierarchy>"""

    assert parse_saved_pages(xml) == [
        {
            "title": "Example",
            "displayed_address": "example.test",
            "description": "Saved page description",
        }
    ]


def test_parse_bookmarks_keeps_visible_bookmark_titles():
    xml = """<hierarchy>
      <node resource-id="com.sec.android.app.sbrowser:id/displayed_view" clickable="true">
        <node resource-id="com.sec.android.app.sbrowser:id/bookmark_folder_title" text="Example" />
      </node>
    </hierarchy>"""

    assert parse_bookmarks(xml) == [{"title": "Example"}]


def test_return_to_main_does_nothing_when_main_screen_is_visible():
    context = SimpleNamespace(
        app_profile=SimpleNamespace(
            selectors={
                "samsung_browser.main": {
                    "selector": {
                        "kind": "resource_id",
                        "owner_package": "com.sec.android.app.sbrowser",
                        "value": "com.sec.android.app.sbrowser:id/action_more",
                    }
                }
            }
        ),
        device=SimpleNamespace(exists=lambda selector: True),
    )

    assert _return_to_main(context) is None


def test_samsung_browser_is_registered_with_browser_condition():
    assert cli.APP_PACKAGES["samsung_browser"] == "com.sec.android.app.sbrowser"
    assert cli.COLLECTORS["samsung_browser"] is not None
    assert cli._condition(
        "samsung_browser", cli.Route.MATERIALIZE, "device_only"
    )["target"] == {
        "kind": "browser",
        "ref": "browser.samsung-browser.all",
    }


def test_samsung_browser_profile_declares_four_device_only_collections():
    root = Path(__file__).resolve().parents[1] / "profiles"
    profile = ProfileStore(root).load_app(
        "samsung_browser", "30.0.0.67"
    )

    assert [target.target_id for target in profile.targets] == [
        "samsung_browser.history",
        "samsung_browser.downloads",
        "samsung_browser.saved_pages",
        "samsung_browser.bookmarks",
    ]
    assert all(target.acquisition_environments == ("device_only",) for target in profile.targets)
