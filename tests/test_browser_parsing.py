"""Profile-driven extraction must preserve record/selector semantics."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from aura.apps.chrome import collector as chrome
from aura.apps.samsung_browser import collector as samsung
from aura.profiles import ProfileError, ProfileStore
from fakes import browser_profile


@pytest.mark.parametrize('app,version', [('chrome', '156.0.8078.79'), ('samsung_browser', '30.0.5.27')])
def test_browser_profile_requires_all_parsing_rules(app, version):
    from pathlib import Path
    p = ProfileStore(Path(__file__).parents[1] / 'profiles').load_app(app, version)
    for parameters in ({}, {**deepcopy(p.parameters), 'list_parsing': {
            k: v for k, v in p.parameters['list_parsing'].items() if k != 'history'}}):
        with pytest.raises(ProfileError):
            ProfileStore._validate_parser_parameters(app, parameters)


@pytest.mark.parametrize('parser,kwargs', [
    (chrome.parse_history, {}), (chrome.parse_downloads, {}),
    (chrome.parse_bookmarks, {'folder_path': ()}), (chrome.parse_recent_tabs, {}),
    (chrome.parse_account, {}), (samsung.parse_history, {}), (samsung.parse_downloads, {}),
    (samsung.parse_bookmarks, {}), (samsung.parse_saved_pages, {}),
])
def test_browser_parser_requires_explicit_profile(parser, kwargs):
    with pytest.raises(TypeError, match='profile'):
        parser('<hierarchy/>', **kwargs)
    with pytest.raises(AttributeError, match='parameters'):
        parser('<hierarchy/>', profile=None, **kwargs)
    with pytest.raises(KeyError, match='list_parsing'):
        parser('<hierarchy/>', profile=SimpleNamespace(parameters={}), **kwargs)


def profile(views, fields=None):
    return SimpleNamespace(parameters={'list_parsing': views, 'ui_fields': fields or {}})


@pytest.mark.parametrize('parser,prefix', [
    (chrome.parse_downloads, 'com.android.chrome:id/'),
    (samsung.parse_downloads, 'com.sec.android.app.sbrowser:id/'),
])
def test_both_browsers_use_profile_rows_fields_and_groups(parser, prefix):
    spec = {
        'row': {'resource_id': 'entry'},
        'fields': {'title': ['label', {'resource_id': 'preview', 'attribute': 'content-desc'}],
                   'caption': 'detail'},
        'required': ['title'],
        'group': {'selector': {'resource_id': 'heading'}, 'source': {'attribute': 'text'},
                  'field': 'date_group', 'keep_empty': False, 'exclusive': False},
    }
    xml = f'''<hierarchy><node resource-id="{prefix}heading" text="Today"/>
    <node resource-id="{prefix}entry"><node resource-id="{prefix}label" text=""/>
      <node resource-id="{prefix}preview" content-desc="same"/></node>
    <node resource-id="{prefix}entry"><node resource-id="{prefix}label" text="same"/></node>
    </hierarchy>'''
    assert parser(xml, profile=profile({'downloads': spec})) == [
        {'date_group': 'Today', 'title': 'same', 'caption': ''},
        {'date_group': 'Today', 'title': 'same', 'caption': ''},
    ]


def test_bookmark_profile_classifies_empty_folder_marker_and_preserves_actions():
    spec = deepcopy(browser_profile('chrome', '156.0.8078.79').parameters['list_parsing']['bookmarks'])
    spec['kind']['exists'] = 'folder_marker'
    spec['variants']['folder'] = {'child_count': 'folder_marker'}
    p = profile({'bookmarks': spec})
    xml = '''<hierarchy>
      <node clickable="true" content-desc="same"><node resource-id="title" text="Folder"/>
        <node resource-id="folder_marker" text=""/></node>
      <node clickable="true" content-desc="same"><node resource-id="title" text="Folder"/>
        <node resource-id="folder_marker" text="2"/></node>
      <node clickable="true"><node resource-id="title" text="Leaf"/></node>
    </hierarchy>'''
    rows = chrome._bookmark_rows(xml, (), profile=p)
    assert [r['kind'] for r, _ in rows] == ['folder', 'folder', 'bookmark']
    assert 'child_count' not in rows[0][0]
    assert [s['instance'] for _, s in rows[:2]] == [0, 1]
    assert rows[2][1] is None


def test_recent_tabs_profile_exclusion_distinguishes_missing_and_empty_domain():
    spec = deepcopy(browser_profile('chrome', '156.0.8078.79').parameters['list_parsing']['recent_tabs'])
    spec['exclude']['equals'] = 'All visits'
    xml = '''<hierarchy>
      <node resource-id="recent_tabs_list_item_layout"><node resource-id="title_row" text="All visits"/></node>
      <node resource-id="recent_tabs_list_item_layout"><node resource-id="title_row" text="All visits"/>
        <node resource-id="domain_row" text=""/></node>
    </hierarchy>'''
    assert chrome.parse_recent_tabs(xml, profile=profile({'recent_tabs': spec})) == [
        {'group': '', 'title': 'All visits', 'domain': ''}]


def test_invalid_or_missing_parser_section_rejected():
    with pytest.raises(ProfileError, match='ui_fields'):
        ProfileStore._validate_parser_parameters('chrome', {})
    for bad in ([], {'history': {'row': {}, 'fields': {}, 'required': ['missing']}},
                {'history': {'row': {}, 'fields': {'title': {'attribute': 42}}}}):
        with pytest.raises(ProfileError, match='list_parsing'):
            ProfileStore._validate_parser_parameters('chrome', {**browser_profile('chrome', '156.0.8078.79').parameters, 'list_parsing': bad})


@pytest.mark.parametrize('change', ['unknown_view', 'missing_sections', 'renamed_output', 'wrong_kind',
                                  'optional_caption', 'optional_title'])
def test_browser_profile_rejects_incompatible_output_contract(change):
    views = deepcopy(browser_profile('chrome', '156.0.8078.79').parameters['list_parsing'])
    if change == 'unknown_view':
        views['bookmark_typo'] = views.pop('bookmarks')
    elif change == 'missing_sections':
        del views['bookmarks']['sections']
    elif change == 'renamed_output':
        views['downloads']['fields']['renamed'] = views['downloads']['fields'].pop('caption')
    elif change == 'optional_caption':
        views['downloads']['optional'] = ['caption']
    elif change == 'optional_title':
        views['bookmarks']['required'] = []
        views['bookmarks']['optional'] = ['title']
    else:
        views['bookmarks']['kind']['present'] = 'directory'
        views['bookmarks']['variants']['directory'] = views['bookmarks']['variants'].pop('folder')
        views['bookmarks']['action']['kind'] = 'directory'
    with pytest.raises(ProfileError, match='list_parsing'):
        ProfileStore._validate_parser_parameters('chrome', {**browser_profile('chrome', '156.0.8078.79').parameters, 'list_parsing': views})


def test_duplicate_optional_fields_are_rejected_before_account_parsing():
    views = deepcopy(browser_profile('chrome', '156.0.8078.79').parameters['list_parsing'])
    views['account']['optional'] = ['email', 'email']
    with pytest.raises(ProfileError, match='list_parsing'):
        ProfileStore._validate_parser_parameters('chrome', {**browser_profile('chrome', '156.0.8078.79').parameters, 'list_parsing': views})


@pytest.mark.parametrize('app,version,collector', [
    ('chrome', '156.0.8078.79', chrome),
    ('samsung_browser', '30.0.5.27', samsung),
])
def test_identification_rule_binds_actual_parser_definition(app, version, collector):
    from dataclasses import replace
    from pathlib import Path
    p = ProfileStore(Path(__file__).parents[1] / 'profiles').load_app(app, version)
    params = deepcopy(p.parameters)
    params['list_parsing']['downloads']['row'] = {'resource_id': 'new_download_row'}
    changed = replace(p, parameters=params)
    ProfileStore._validate_parser_parameters(app, params)
    with pytest.raises(ProfileError, match='UI criteria'):
        changed.require_identification_rules(collector.identification_contracts(changed))
    targets = deepcopy(changed.targets)
    contracts = collector.identification_contracts(changed)
    for target in targets:
        target.rules['identification']['parameters']['ui_criteria'] = contracts[target.target_id][1]
    updated = replace(changed, targets=targets)
    updated.require_identification_rules(contracts)
    criterion = contracts[app + '.downloads'][1]
    assert criterion['parser_spec']['parameter'] == 'list_parsing.downloads'
    assert 'row_resource_id' not in criterion  # No stale second copy of selector data.
    title_id = 'title' if app == 'chrome' else 'download_item_title'
    assert collector.parse_downloads(f'<hierarchy><node resource-id="new_download_row">'
        f'<node resource-id="{title_id}" text="File"/></node></hierarchy>', profile=updated)[0]['title'] == 'File'
