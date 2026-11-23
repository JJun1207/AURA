"""Recent tabs uses the Chrome menu, including from a start page without a URL bar."""
from functools import partial
from dataclasses import replace
from pathlib import Path

import pytest

from aura.apps.chrome import collector as chrome
from aura.artifacts import ArtifactStore
from aura.journal import EventJournal
from aura.profiles import ProfileStore
from aura.runtime import RunContext
from aura.ui import UiRuntime, UiTimeout
from fakes import FakeDevice


class ChromeDevice(FakeDevice):
    def __init__(self, *, page="original", navigate=True, menu_available=True):
        super().__init__()
        self.page = page
        self.surface = 'page'
        self.tabs = 14
        self.home_clicks = 0
        self.menu_available = menu_available
        self.navigate = navigate
        self.offset = 1 if page == 'recent' else 0

    def count(self, selector):
        wanted = selector.get('resourceId', '').rsplit('/', 1)[-1]
        visible = {
            'page': {'menu_button', 'home_button'} | ({'search_box_text'} if self.page == 'start' else {'url_bar'}),
            'menu': {'app_menu_list', 'recent_tabs_menu_id', 'open_history_menu_id', 'preferences_id'},
            'history': {'close_menu_id', 'history_page_recycler_view'},
            'settings': {'account_management_account_row'},
            'bookmarks': {'close_menu_id', 'selectable_list'},
        }[self.surface]
        if self.page == 'recent' and self.surface == 'page':
            visible |= {'recent_tabs_root'}
        if not self.menu_available:
            visible.discard('recent_tabs_menu_id')
        return int(wanted in visible)

    def click(self, selector):
        super().click(selector)
        name = selector['resourceId'].rsplit('/', 1)[-1]
        if name == 'menu_button':
            self.surface = 'menu'
        elif name == 'recent_tabs_menu_id':
            self.tabs += 1  # Chromium's native menu opens a new tab.
            if self.navigate:
                self.page, self.surface, self.offset = 'recent', 'page', 0
        elif name == 'home_button':
            self.home_clicks += 1
            self.page = 'home'
        elif name == 'open_history_menu_id':
            self.surface = 'history'
        elif name == 'preferences_id':
            self.surface = 'settings'
        elif name == 'close_menu_id':
            self.surface = 'page'

    def submit_text(self, selector, text):
        raise AssertionError("Recent tabs must not enter an internal URL")

    def back(self):
        self.back_count += 1
        if self.surface != 'page':
            self.surface = 'page'

    def swipe(self, direction, duration=0.2):
        self.swipes.append((direction, duration))
        if direction == 'down':
            self.offset = 0

    def hierarchy(self):
        titles = ['Older'] if self.offset else ['Newest', 'Older']
        rows = ''.join(f'<node resource-id="com.android.chrome:id/recent_tabs_list_item_layout">'
                       f'<node resource-id="com.android.chrome:id/title_row" text="{title}"/></node>' for title in titles)
        return '<hierarchy>' + (rows if self.page == 'recent' else '') + '</hierarchy>'


def context_for(tmp_path, device):
    profiles = ProfileStore(Path(__file__).resolve().parents[1] / 'profiles')
    profile = replace(profiles.load_app('chrome', '156.0.8078.79'), timings={**profiles.load_app('chrome', '156.0.8078.79').timings, "scroll_settle": 0, 'transition_settle': 0})
    journal = EventJournal(tmp_path / 'events.jsonl')
    return RunContext(run_dir=tmp_path, base_context={}, device=device, journal=journal,
                      artifacts=ArtifactStore(tmp_path, journal),
                      ui=UiRuntime(device, journal, default_timeout=0, poll_interval=0),
                      app_profile=profile, system_ui_profile=profiles.load_system_ui('samsung'))


def collect_recent(context):
    return chrome._collect_list(context, surface='recent-tabs', target_id='chrome.recent_tabs',
                                item_type='recent_tab_collection', menu_item='chrome.menu.recent-tabs',
                                marker='chrome.screen.recent-tabs', parser=partial(chrome.parse_recent_tabs, profile=context.app_profile))


@pytest.mark.parametrize("initial", ["original", "start", "bookmarks", "recent"])
def test_recent_tabs_uses_menu_from_each_starting_surface(tmp_path, initial):
    device = ChromeDevice(page="start" if initial == "bookmarks" else initial)
    if initial == "bookmarks":
        device.surface = "bookmarks"
    count, _ = collect_recent(context_for(tmp_path, device))
    assert count == 2
    names = [click["resourceId"].rsplit("/", 1)[-1] for click in device.clicks]
    assert names == (["close_menu_id"] if initial == "bookmarks" else []) + ["menu_button", "recent_tabs_menu_id"]
    assert device.tabs == 15 and device.page == "recent"
    assert device.home_clicks == 0 and device.back_count == 0


def test_repeated_collection_keeps_normal_menu_behavior(tmp_path):
    device = ChromeDevice(page="start")
    for number in range(2):
        count, _ = collect_recent(context_for(tmp_path / str(number), device))
        assert count == 2
        assert device.tabs == 15 + number
    assert device.back_count == 0


@pytest.mark.parametrize("failure", ["missing_menu_item", "page_not_opened"])
def test_failed_recent_tabs_entry_preserves_failure_without_address_fallback(tmp_path, failure):
    device = ChromeDevice(page="start", menu_available=failure != "missing_menu_item",
                          navigate=failure != "page_not_opened")
    context = context_for(tmp_path, device)
    with pytest.raises(UiTimeout, match="open_chrome_menu_recent-tabs"):
        collect_recent(context)
    assert context.attempts[0].procedure_status.value == "interrupted"
    assert (tmp_path / "artifacts/chrome/recent-tabs/failure.xml").exists()
    assert device.back_count == 0 and device.home_clicks == 0


def test_history_opens_from_existing_page_without_home_navigation(tmp_path):
    device = ChromeDevice()
    chrome._open_surface(context_for(tmp_path, device), 'chrome.menu.history', 'chrome.screen.history')
    assert device.surface == 'history' and device.page == 'original'
    assert device.home_clicks == 0


def test_settings_activity_is_left_before_next_surface(tmp_path):
    device = ChromeDevice()
    device.surface = 'settings'
    chrome._open_surface(context_for(tmp_path, device), 'chrome.menu.history', 'chrome.screen.history')
    assert device.surface == 'history'
    assert device.back_count == 1 and device.home_clicks == 0
