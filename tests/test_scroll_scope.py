from dataclasses import replace
from itertools import count
from pathlib import Path
from types import SimpleNamespace

from aura.apps.notesnook.adapter import NotesnookDeviceAdapter
from aura.apps.notion import pages
from aura.profiles import ProfileStore


def test_notesnook_only_explicit_list_gesture_uses_larger_distance():
    calls = []
    def swipe(*args, **kwargs):
        calls.append(kwargs.get('distance_ratio'))
        return SimpleNamespace(event_id='event', action='scroll')
    profile = ProfileStore(Path(__file__).resolve().parents[1] / 'profiles').load_app('notesnook', '3.4.15')
    profile = replace(profile, timings={**profile.timings, 'transition_settle': 0})
    # Also exercise the previous broad-binding implementation in the RED check.
    profile.parameters['scroll_distance_ratio'] = .3
    adapter = NotesnookDeviceAdapter(SimpleNamespace(app_profile=profile, ui=SimpleNamespace(swipe=swipe)))
    adapter.swipe('attachment-list', 'up', 'obs')
    adapter.swipe('list-rewind', 'down', 'obs')
    assert calls == [None, None]
    adapter.swipe('note-list', 'up', 'obs', distance_ratio=.3)
    assert calls == [None, None, .3]


def test_notion_only_forward_page_content_uses_larger_distance(monkeypatch):
    calls = []
    def swipe(*args, **kwargs):
        calls.append(kwargs.get('distance_ratio'))
        return SimpleNamespace()
    ctx = SimpleNamespace(ui=SimpleNamespace(swipe=swipe),
        app_profile=SimpleNamespace(parameters={'page_scroll_ratio': .3, 'scroll_distance_ratio': .3}))
    monkeypatch.setattr(pages, 'observe', lambda *args, **kwargs: {})
    seq = count()
    pages._swipe_observe(ctx, seq, 'workspace', 'up')
    pages._swipe_page_observe(ctx, seq, 'page', 'down')
    pages._swipe_page_observe(ctx, seq, 'page', 'up')
    assert calls == [None, None, .3]
