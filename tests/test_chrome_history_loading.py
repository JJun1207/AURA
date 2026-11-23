"""History can show its empty view before asynchronous loading starts."""
from functools import partial
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from aura.apps.chrome import collector as chrome
from aura.artifacts import ArtifactStore
from aura.journal import EventJournal
from aura.profiles import ProfileStore, ProfileError
from aura.runtime import RunContext
from aura.ui import UiRuntime, UiTimeout
from fakes import FakeDevice


def window(titles=(), *, spinner=None, empty=False, hidden=False):
    rows = ''.join('<node clickable="true"><node resource-id="com.android.chrome:id/title" '
                   f'text="{title}"/><node resource-id="com.android.chrome:id/description" '
                   f'text="{title}.test"/></node>' for title in titles)
    progress = (f'<node resource-id="com.android.chrome:id/{spinner}" '
                f'visible-to-user="{str(not hidden).lower()}"/>') if spinner else ''
    message = ('<node resource-id="com.android.chrome:id/empty_state_text_title" '
               'text="You will find your history here"/>') if empty else ''
    return ('<hierarchy><node package="com.android.chrome">'
            '<node resource-id="com.android.chrome:id/history_page_recycler_view">'
            + rows + '</node>' + progress + message + '</node></hierarchy>')


def collect(tmp_path, monkeypatch, scenario, *, renamed_fields=None):
    clock = SimpleNamespace(now=0.0)
    def sleep(seconds):
        clock.now += seconds
    monkeypatch.setattr(chrome.time, 'sleep', sleep)

    class Device(FakeDevice):
        page = 0
        loading_since = None
        swiped_during_loading = False

        def hierarchy(self):
            if scenario == 'delayed' and clock.now < 12:
                return window(empty=True, spinner='loading_view' if clock.now >= 10 else None)
            if scenario == 'never_loaded':
                return window(spinner='loading_view', empty=True)
            if scenario == 'unresolved_empty':
                return window(empty=True)
            if scenario == 'confirmed_empty':
                return window(empty=True, spinner='loading_view' if clock.now < 3 else None)
            if scenario in ('pagination', 'pagination_timeout') and self.page:
                loading = scenario == 'pagination_timeout' or clock.now - self.loading_since < 8
                return window('AB' if loading else 'BC', spinner='progress_spinner' if loading else None)
            return window('AB', spinner='loading_view' if scenario == 'hidden_spinner' else None, hidden=True)

        def swipe(self, *args):
            if scenario == 'delayed' and clock.now < 12:
                self.swiped_during_loading = True
            if self.page == 0:
                self.loading_since = clock.now
            self.page += 1
            super().swipe(*args)

    device = Device()
    if renamed_fields:
        original = device.hierarchy
        def renamed_hierarchy():
            xml = original()
            for old, new in renamed_fields.items():
                xml = xml.replace(f':id/{old}"', f':id/{new}"')
            return xml
        device.hierarchy = renamed_hierarchy
    store = ProfileStore(Path(__file__).resolve().parents[1] / 'profiles')
    profile = replace(store.load_app('chrome', '156.0.8078.79'), timings={**store.load_app('chrome', '156.0.8078.79').timings, "scroll_settle": 0.4,
        'transition_settle': 0.4, 'history_load_timeout': 30,
    })
    if renamed_fields:
        profile = replace(profile, parameters={**profile.parameters, 'ui_fields': {
            **profile.parameters['ui_fields'], **renamed_fields}})
    journal = EventJournal(tmp_path / 'events.jsonl')
    context = RunContext(run_dir=tmp_path, base_context={}, device=device, journal=journal,
                         artifacts=ArtifactStore(tmp_path, journal), app_profile=profile,
                         system_ui_profile=store.load_system_ui('samsung'),
                         ui=UiRuntime(device, journal, default_timeout=5, poll_interval=0.2,
                                      monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(chrome, '_open_surface', lambda *args: journal.record_action('open_history', status='success'))
    def run():
        return chrome._collect_list(context, surface='history', target_id='chrome.history',
                                    item_type='history_collection', menu_item='unused', marker='unused',
                                    parser=partial(chrome.parse_history, profile=context.app_profile))
    return run, context, device, clock


@pytest.mark.parametrize('scenario,count', [('confirmed_empty', 0), ('pagination', 3)])
def test_history_readiness_uses_profile_loading_and_empty_fields(tmp_path, monkeypatch, scenario, count):
    run, context, device, clock = collect(tmp_path, monkeypatch, scenario, renamed_fields={
        'loading_view': 'version_loading', 'progress_spinner': 'version_progress',
        'empty_state_text_title': 'version_empty'})
    actual, _ = run()
    assert actual == count
    assert clock.now >= (3 if scenario == 'confirmed_empty' else 8)
    context.validate_records()


def test_loading_fields_are_optional_for_saved_profiles_but_validated():
    import copy
    import json
    parameters = json.loads((Path(__file__).parents[1] / 'profiles/apps/chrome/156.0.8078.79.json').read_text())['parameters']
    ProfileStore._validate_parser_parameters('chrome', parameters)
    legacy = copy.deepcopy(parameters)
    for name in ('loading_view', 'progress_spinner', 'empty_state_text_title'):
        legacy['ui_fields'].pop(name)
    ProfileStore._validate_parser_parameters('chrome', legacy)
    for key, value in [('loading_view', ''), ('loading_typo', 'loading')]:
        invalid = copy.deepcopy(parameters)
        invalid['ui_fields'][key] = value
        with pytest.raises(ProfileError, match='invalid ui_fields'):
            ProfileStore._validate_parser_parameters('chrome', invalid)


@pytest.mark.parametrize('scenario,count', [('delayed', 2), ('pagination', 3),
                                           ('confirmed_empty', 0), ('hidden_spinner', 2)])
def test_history_waits_for_loaded_content_without_scrolling_loading_screens(tmp_path, monkeypatch, scenario, count):
    run, context, device, clock = collect(tmp_path, monkeypatch, scenario)
    actual, _ = run()
    assert actual == count
    assert not device.swiped_during_loading
    assert context.attempts[0].procedure_status.value == 'completed'
    context.validate_records()
    if scenario == 'delayed':
        assert clock.now >= 12
        assert len(device.swipes) == 1
    if scenario == 'confirmed_empty':
        assert clock.now >= 3
        assert not device.swipes


@pytest.mark.parametrize('scenario,retained', [('never_loaded', 0), ('unresolved_empty', 0),
                                             ('pagination_timeout', 2)])
def test_history_loading_timeout_preserves_partial_instead_of_completing_empty(tmp_path, monkeypatch, scenario, retained):
    run, context, device, clock = collect(tmp_path, monkeypatch, scenario)
    with pytest.raises(UiTimeout, match='chrome_history_content_not_ready'):
        run()
    assert context.attempts[0].procedure_status.value == 'interrupted'
    assert context.attempts[0].details['record_count'] == retained
    assert context.identifications[0].traversal_complete is False
    assert (tmp_path / 'artifacts/chrome/history/failure.xml').exists()
    assert clock.now >= 30
    context.validate_records()
