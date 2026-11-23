"""Required execution settings are checked while loading, before device work."""
import json
from pathlib import Path

import pytest

from aura.profiles import ProfileError, ProfileStore

ROOT = Path(__file__).resolve().parents[1]
PROFILES = sorted((ROOT / 'profiles/apps').glob('*/*.json'))


@pytest.mark.parametrize('path', PROFILES, ids=lambda p: f'{p.parent.name}-{p.stem}')
def test_missing_execution_timing_rejected_at_load(tmp_path, path):
    original = json.loads(path.read_text())
    dest = tmp_path / path.relative_to(ROOT / 'profiles')
    dest.parent.mkdir(parents=True)
    for key in original['timings']:
        document = {**original, 'timings': {k: v for k, v in original['timings'].items() if k != key}}
        dest.write_text(json.dumps(document))
        with pytest.raises(ProfileError, match='timings'):
            ProfileStore(tmp_path).load_app(original['app_id'], original['app_version'])


@pytest.mark.parametrize('value', [float('inf'), float('nan')])
def test_nonfinite_timing_rejected(tmp_path, value):
    path = ROOT / 'profiles/apps/chrome/156.0.8078.79.json'
    document = json.loads(path.read_text())
    document['timings']['default_timeout'] = value
    dest = tmp_path / path.relative_to(ROOT / 'profiles')
    dest.parent.mkdir(parents=True)
    dest.write_text(json.dumps(document))
    with pytest.raises(ProfileError, match='timings'):
        ProfileStore(tmp_path).load_app('chrome', '156.0.8078.79')


@pytest.mark.parametrize('path', PROFILES, ids=lambda p: f'{p.parent.name}-{p.stem}')
def test_missing_parser_settings_rejected_at_load(tmp_path, path):
    original = json.loads(path.read_text())
    dest = tmp_path / path.relative_to(ROOT / 'profiles')
    dest.parent.mkdir(parents=True)
    for section in ('ui_fields', 'display_text', 'display_patterns'):
        if not original['parameters'].get(section):
            continue
        document = {**original, 'parameters': {k: v for k, v in original['parameters'].items() if k != section}}
        dest.write_text(json.dumps(document))
        with pytest.raises(ProfileError, match=section):
            ProfileStore(tmp_path).load_app(original['app_id'], original['app_version'])


def test_field_lookup_does_not_supply_an_absent_profile_mapping():
    from types import SimpleNamespace
    from aura.collection import field_id
    with pytest.raises(KeyError, match='ui_fields'):
        field_id(SimpleNamespace(parameters={}), 'title')
