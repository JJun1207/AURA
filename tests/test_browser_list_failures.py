"""Synthetic UI regressions for browser list preservation; no device required."""
from functools import partial

from dataclasses import replace
import json
from pathlib import Path

import pytest

from aura.apps.chrome import collector as chrome
from aura.apps.samsung_browser import collector as samsung
from aura.models import Outcome, OutcomeStatus, Route
from aura.profiles import ProfileStore
from aura.runtime import AcquisitionRunError, AcquisitionRuntime
from fakes import FakeDevice


LISTS = [("chrome", "downloads"), ("chrome", "recent-tabs"),
         ("samsung_browser", "downloads"), ("samsung_browser", "saved-pages")]


def window(app, surface, titles):
    prefix = chrome.PREFIX if app == "chrome" else samsung.PREFIX
    rows = []
    for title in titles:
        if app == "chrome" and surface == "downloads":
            row, fields = "row", {"title": title, "caption": "1 B • same.test"}
        elif app == "chrome":
            row, fields = "recent_tabs_list_item_layout", {"title_row": title, "domain_row": "same.test"}
        elif surface == "downloads":
            row, fields = "download_item_parent", {"download_item_title": title, "download_item_url": "same.test", "download_item_total_size": "1 B"}
        else:
            row, fields = "saved_page_list_view_layout", {"saved_page_list_view_title_text_view": title, "saved_page_list_view_url_text_view": "same.test", "saved_page_list_view_description_text_view": "1 B"}
        rows.append(f'<node resource-id="{prefix}{row}" clickable="true">' + "".join(
            f'<node resource-id="{prefix}{name}" text="{value}"/>' for name, value in fields.items()
        ) + "</node>")
    return "<hierarchy>" + "".join(rows) + "</hierarchy>"


class WindowsDevice(FakeDevice):
    def __init__(self, windows, *, screenshot_error=False):
        super().__init__()
        self.windows, self.index = windows, 0
        self.screenshot_error = screenshot_error

    def hierarchy(self):
        return self.windows[self.index]

    def screenshot(self):
        if self.screenshot_error:
            raise OSError("screenshot unavailable")
        return super().screenshot()

    def swipe(self, *args):
        self.index = min(self.index + 1, len(self.windows) - 1)


def run_list(tmp_path, monkeypatch, app, surface, titles=("ARUR", "RURV"), *, entry_failure=None, screenshot_error=False, timings=None, sleeps=None):
    module = chrome if app == "chrome" else samsung
    store = ProfileStore(Path(__file__).resolve().parents[1] / "profiles")
    profile = store.load_app(app, "156.0.8078.79" if app == "chrome" else "30.0.5.27")
    profile = replace(profile, timings={**dict.fromkeys(profile.timings, 0), **(timings or {})})
    windows = [window(app, surface, names) for names in titles]
    if entry_failure == "bookmark-root":
        windows = ['<hierarchy><node resource-id="com.android.chrome:id/action_bar"><node class="android.widget.TextView" text="Remembered folder"/></node></hierarchy>']
    device = WindowsDevice(windows, screenshot_error=screenshot_error)

    def opener(context, *args):
        if entry_failure == "open":
            return context.ui.click("open_missing_surface", {"resourceId": "missing"}, timeout=0)
        return context.journal.record_action("open", status="success")

    def collect(context):
        if surface == "bookmarks":
            count, action = chrome._collect_bookmarks(context)
        else:
            kwargs = dict(surface=surface, target_id=f"{app}.{surface.replace('-', '_')}",
                          item_type={"downloads": "download_collection", "recent-tabs": "recent_tab_collection", "saved-pages": "saved_page_collection"}[surface],
                          parser=partial(getattr(module, "parse_" + surface.replace("-", "_")), profile=context.app_profile))
            if app == "chrome":
                count, action = chrome._collect_list(context, **kwargs, menu_item="unused", marker="unused")
            else:
                count, action = samsung._collect_list(context, **kwargs, opener=lambda: opener(context))
        return Outcome(OutcomeStatus.COMPLETE, action=action, details={"record_count": count})

    monkeypatch.setattr(chrome, "_open_surface", opener)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: sleeps.append(seconds) if sleeps is not None else None)
    try:
        result = AcquisitionRuntime(tmp_path, device).run(profile, store.load_system_ui("samsung"), Route.MATERIALIZE,
            {"acquisition_environment": "device_only"}, collect, run_id="browser-list")
        run_dir = result.run_dir
    except AcquisitionRunError as error:
        run_dir = error.run_dir
    document = json.loads((run_dir / "acquisition.json").read_text())
    app_dir = "chrome" if app == "chrome" else "samsung-browser"
    payload_path = run_dir / f"artifacts/{app_dir}/{surface}.json"
    assert payload_path.exists(), "list failure must retain the records collected so far"
    payload = json.loads(payload_path.read_text())
    return document, payload, run_dir


@pytest.mark.parametrize('app', ['chrome', 'samsung_browser'])
def test_scroll_wait_is_independent_of_navigation(tmp_path, monkeypatch, app):
    sleeps = []
    document, payload, _ = run_list(tmp_path, monkeypatch, app, 'downloads', titles=('AB', 'BC'),
        timings={'transition_settle': .8, 'scroll_settle': .125}, sleeps=sleeps)
    assert [row['title'] for row in payload['records']] == ['A', 'B', 'C']
    assert .125 in sleeps
    assert .8 not in sleeps  # Navigation is stubbed; only list scrolling waits here.


@pytest.mark.parametrize("app,surface", LISTS)
@pytest.mark.parametrize("titles,expected,complete", [
    (("ARUR", "RURV", "VWX"), "ARURVWX", True),
    (("ABCD", "EFGH", "GHI"), "ABCDEFGHI", False),
    (("AABC", "BCAA"), "AABCAA", True),
])
def test_lists_merge_longest_overlap_and_preserve_unmatched_windows(tmp_path, monkeypatch, app, surface, titles, expected, complete):
    document, payload, _ = run_list(tmp_path, monkeypatch, app, surface, titles)
    assert "".join(row["title"] for row in payload["records"]) == expected
    target = next(row for row in document["identifications"] if row["target_id"] == f"{app}.{surface.replace('-', '_')}")
    assert target["traversal_complete"] is True
    assert document["attempts"][0]["procedure_status"] == "completed"
    assert document["attempts"][0]["acquisition_status"] == ("acquired" if complete else "partial")
    if not complete:
        assert "overlap_unconfirmed" in document["attempts"][0]["reason"]
        assert len(document["observations"]) == 3
        warning, = payload["unconfirmed_overlaps"]
        assert warning["window"] == 2
        assert warning["record_start_index"] == 4
        assert warning["record_count"] == 4
        observations = {o["observation_id"] for o in document["observations"]}
        assert {warning["previous_observation_id"], warning["observation_id"]} <= observations
        assert payload["record_count_semantics"] == "retained_rows_may_include_window_overlap"


@pytest.mark.parametrize("app,surface", LISTS)
def test_nonadjacent_equal_window_is_not_the_end(tmp_path, monkeypatch, app, surface):
    document, payload, _ = run_list(tmp_path, monkeypatch, app, surface,
                                    titles=("AB", "BC", "AB", "BD"))
    assert "".join(row["title"] for row in payload["records"]) == "ABCABD"
    assert document["attempts"][0]["procedure_status"] == "completed"


@pytest.mark.parametrize("app,surface,entry_failure", [
    ("chrome", "downloads", "open"), ("samsung_browser", "downloads", "open"),
    ("chrome", "bookmarks", "open"), ("chrome", "bookmarks", "bookmark-root"),
])
def test_entry_failure_retains_screen_and_hierarchy_linked_to_attempt(tmp_path, monkeypatch, app, surface, entry_failure):
    document, payload, run_dir = run_list(tmp_path, monkeypatch, app, surface, entry_failure=entry_failure)
    attempt, = document["attempts"]
    assert len(document["observations"]) == 1
    observation, = document["observations"]
    assert observation["attempt_id"] == attempt["attempt_id"]
    assert {artifact["kind"] for artifact in document["artifacts"]} == {"screen_image", "ui_hierarchy", "structured_data"}
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    failed_action = next(event for event in events if event["event_type"] == "action" and event["status"] == "failed")
    observed_action = next(event for event in events if event.get("action_id") == observation["action_id"])
    assert observed_action["context"]["source_action_id"] == failed_action["action_id"]
    outcome = next(row for row in document["outcomes"] if row["attempt_id"] == attempt["attempt_id"])
    assert outcome["observation_id"] == observation["observation_id"]
    assert payload["records"] == []
    assert attempt["acquisition_status"] == "partial"
    assert "target not found" in attempt["reason"]
    assert document["outcome"]["status"] != "complete"


@pytest.mark.parametrize("app", ["chrome", "samsung_browser"])
def test_capture_failure_preserves_original_entry_error_and_records(tmp_path, monkeypatch, app):
    document, payload, _ = run_list(tmp_path, monkeypatch, app, "downloads", entry_failure="open", screenshot_error=True)
    attempt, = document["attempts"]
    assert attempt["acquisition_status"] == "partial"
    assert "target not found" in attempt["reason"]
    assert "screenshot unavailable" in attempt["details"].get("failure_evidence_error", "")
    assert payload["records"] == []


@pytest.mark.parametrize("app,surface", LISTS)
def test_lists_finish_when_only_system_clock_changes(tmp_path, monkeypatch, app, surface):
    original = WindowsDevice.hierarchy
    def with_clock(self):
        return original(self).replace("</hierarchy>",
            f'<node package="com.android.systemui" text="12:{self.index:02d}"/></hierarchy>')
    monkeypatch.setattr(WindowsDevice, "hierarchy", with_clock)
    document, payload, _ = run_list(tmp_path, monkeypatch, app, surface, titles=("A", "A"))
    assert [row["title"] for row in payload["records"]] == ["A"]
    assert document["outcome"]["status"] == "complete"
