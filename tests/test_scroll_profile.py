from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from aura.device import AndroidDevice
from aura.journal import EventJournal
from aura.profiles import ProfileError, ProfileStore
from aura.ui import UiRuntime
from fakes import UnobstructedAdb

ROOT = Path(__file__).resolve().parents[1]


def test_shared_swipe_forwards_profile_geometry_and_records_it(tmp_path):
    calls = []
    client = SimpleNamespace(window_size=lambda: (100, 200), swipe=lambda *args: calls.append(args))
    device = AndroidDevice(client, UnobstructedAdb(), serial=None)
    journal = EventJournal(tmp_path / 'events.jsonl')
    profile = ProfileStore(ROOT / 'profiles').load_app('notesnook', '3.4.15')
    ratio = profile.parameters['note_list_scroll_ratio']
    action = UiRuntime(device, journal).swipe('list', 'up', distance_ratio=ratio)
    assert calls == [(50, 130, 50, 70, .2)]
    event = json.loads(journal.path.read_text().splitlines()[-1])
    assert action.status == 'success' and event['details']['distance_ratio'] == .3


@pytest.mark.parametrize('app,version', [('notesnook','3.4.15'),('notion','0.6.4280')])
@pytest.mark.parametrize('bad', [None, False, 0, 1.01, float('nan')])
def test_scroll_profile_rejects_missing_or_invalid_geometry_before_acquisition(tmp_path, app, version, bad):
    key = {'notesnook':'note_list_scroll_ratio','notion':'page_scroll_ratio'}[app]
    document = json.loads((ROOT / 'profiles/apps' / app / (version+'.json')).read_text())
    if bad is None:
        document['parameters'].pop(key, None)
    else:
        document['parameters'][key] = bad
    path = tmp_path / 'apps' / app / (version+'.json')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document))
    with pytest.raises(ProfileError, match=key):
        ProfileStore(tmp_path).load_app(app, version)


def test_versioned_scroll_ratios_preserve_older_profiles():
    store = ProfileStore(ROOT / 'profiles')
    expected = {
        'notesnook': {'3.4.5': .2, '3.4.15': .3},
        'notion': {'0.6.4030': .2, '0.6.4280': .3, '0.6.4304': .3},
    }
    for app, versions in expected.items():
        assert {p.stem for p in (ROOT / 'profiles/apps' / app).glob('*.json')} == versions.keys()
        for version, ratio in versions.items():
            profile = store.load_app(app, version)
            assert profile.parameters[{'notesnook':'note_list_scroll_ratio','notion':'page_scroll_ratio'}[app]] == ratio
