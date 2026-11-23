import pytest
from aura import collection


def test_overlap_candidates_preserve_ambiguity_and_accept_app_comparison():
    assert collection.overlap_candidates(['a', 'a'], ['a', 'a']) == [1, 2]
    assert collection.overlap_candidates(['A', 'b'], ['B', 'c'],
                                        same=lambda a, b: a.lower() == b.lower()) == [1]


def test_named_regex_fields_are_validated():
    import json
    from pathlib import Path
    from aura.profiles import ProfileStore, ProfileError
    p = Path(__file__).parents[1]/'profiles/apps/telegram/13.1.1.json'
    parameters = json.loads(p.read_text())['parameters']
    parameters['display_patterns']['message_status'] = parameters['display_patterns']['message_status'].replace('(?P<status>', '(')
    with pytest.raises(ProfileError, match='named capture'):
        ProfileStore._validate_parser_parameters('telegram', parameters)


def test_notes_sync_labels_follow_profile_without_changing_environment_gate():
    import xml.etree.ElementTree as E
    from types import SimpleNamespace
    from aura.apps.notesnook.collector import _sync_state
    profile = SimpleNamespace(parameters={'display_text': {
        'sync_failed': 'Failed ', 'syncing': 'Working', 'synced': 'Done ', 'offline': '(No network)'}})
    root = E.fromstring('<node package="com.streetwriters.notesnook" visible-to-user="true" class="android.widget.TextView" text="Failed (No network)" bounds="[0,0][100,100]"/>')
    assert _sync_state(root, 'device_only', profile=profile)['sync_label_matches_environment']
    assert not _sync_state(root, 'controlled_online', profile=profile)['sync_label_matches_environment']


def test_windows_stop_before_retaining_repeated_view_and_keep_duplicates():
    views = iter([['same', 'same'], ['same', 'other'], ['same', 'other']])
    moves = []
    assert list(collection.iter_windows(lambda: next(views), lambda: moves.append('up'), tuple)) == [['same', 'same'], ['same', 'other']]
    assert moves == ['up', 'up']


def test_candidate_visits_reacquire_after_each_return():
    rows = [{'id': 1, 'y': 10}, {'id': 2, 'y': 20}]
    clicked = []
    for current in collection.visit_candidates(list(rows), lambda old: next(x for x in rows if x['id'] == old['id'])):
        clicked.append(current['y'])
        rows[1] = {'id': 2, 'y': 30}
    assert clicked == [10, 30]


def test_hierarchy_propagates_child_return_and_error_through_parent_cleanup():
    events = []
    def level(node):
        events.append(('enter', node))
        try:
            if node == 'root':
                result = yield 'child'
                events.append(('result', result))
            if node == 'broken':
                raise ValueError('child failed')
            return node + '-done'
        finally:
            events.append(('leave', node))
    assert collection.walk_hierarchy('root', level) == 'root-done'
    assert events == [('enter','root'),('enter','child'),('leave','child'),('result','child-done'),('leave','root')]
    def broken_parent(node):
        try:
            if node == 'root':
                yield 'broken'
            else:
                raise ValueError('child failed')
        finally:
            events.append(('cleanup',node))
    with pytest.raises(ValueError, match='child failed'):
        collection.walk_hierarchy('root', broken_parent)
    assert events[-2:] == [('cleanup','broken'),('cleanup','root')]


def test_profile_fields_change_without_parser_changes():
    import xml.etree.ElementTree as E
    row = E.fromstring('<node><node resource-id="pkg:id/new_title" text="Title"/><node resource-id="pkg:id/check" checked="false"/></node>')
    assert collection.read_fields(row, {'title': {'resource_id': 'pkg:id/new_title'}, 'checked': {'resource_id':'pkg:id/check','attribute':'checked'}}) == {'title': 'Title', 'checked':'false'}
    assert collection.read_fields(row, {'missing': {'resource_id':'absent'}}) == {'missing':''}


def test_shipped_samsung_history_requires_expanded_groups_without_visit_selection():
    from pathlib import Path
    from aura.profiles import ProfileStore
    profile = ProfileStore(Path(__file__).resolve().parents[1] / 'profiles').load_app('samsung_browser','30.0.5.27')
    assert 'history_selection' not in profile.parameters
    target = next(t for t in profile.targets if t.target_id == 'samsung_browser.history')
    assert target.rules['traversal_completion']['parameters']['completion_conditions'] == ['repeated_hierarchy_with_expanded_date_groups']


def test_stable_polling_resets_when_candidate_disappears():
    assert collection.first_stable(iter([('a',1), None, ('a',1), ('a',2), ('a',2)])) == ('a',2)
    assert collection.first_stable(iter([('a',1), None, ('a',1)])) is None


def test_bounded_samples_sleep_only_between_probes():
    calls=[]
    assert list(collection.poll_samples(lambda: 'x', probes=3, interval=.5, sleep=calls.append)) == ['x']*3
    assert calls == [.5,.5]


def test_browser_parsers_follow_version_field_mapping():
    from dataclasses import replace
    from fakes import browser_profile
    from aura.apps.chrome.collector import parse_downloads
    from aura.apps.samsung_browser.collector import parse_history
    profile = browser_profile('chrome', '156.0.8078.79')
    profile = replace(profile, parameters={**profile.parameters, 'ui_fields': {**profile.parameters['ui_fields'], 'title': 'new_title'}})
    xml = '<hierarchy><node clickable="true"><node resource-id="pkg:id/new_title" text="file.txt"/></node></hierarchy>'
    assert parse_downloads(xml, profile=profile)[0]['title'] == 'file.txt'
    profile = browser_profile('samsung_browser', '30.0.5.27')
    profile = replace(profile, parameters={**profile.parameters, 'ui_fields': {
        **profile.parameters['ui_fields'], 'history_relative': 'visit', 'history_title': 'label', 'history_url': 'address'}})
    xml = '<hierarchy><node resource-id="com.sec.android.app.sbrowser:id/visit"><node resource-id="com.sec.android.app.sbrowser:id/label" text="A"/><node resource-id="com.sec.android.app.sbrowser:id/address" text="a.test"/></node></hierarchy>'
    assert parse_history(xml, profile=profile)[0]['displayed_address'] == 'a.test'


@pytest.mark.parametrize('app,version,target,logical,criterion', [
    ('chrome', '156.0.8078.79', 'chrome.account', 'account_management_account_row', 'row_resource_id'),
    ('samsung_browser', '30.0.5.27', 'samsung_browser.history', 'history_relative', 'row_resource_id'),
])
def test_browser_rule_contract_tracks_the_executed_field_mapping(app, version, target, logical, criterion):
    import copy
    import importlib
    from dataclasses import replace
    from pathlib import Path
    from types import SimpleNamespace
    from aura.profiles import ProfileStore, ProfileError
    module = importlib.import_module(f'aura.apps.{app}.collector')
    profile = ProfileStore(Path(__file__).parents[1]/'profiles').load_app(app, version)
    parameters = copy.deepcopy(profile.parameters)
    parameters['ui_fields'][logical] = 'renamed_row'
    stale = replace(profile, parameters=parameters)
    with pytest.raises(ProfileError, match='UI criteria'):
        module.collect(SimpleNamespace(app_profile=stale, base_context={}))
    targets = copy.deepcopy(stale.targets)
    # New profiles bind their extraction definition and field map together.
    contracts = module.identification_contracts(stale)
    for t in targets:
        t.rules['identification']['parameters']['ui_criteria'] = contracts[t.target_id][1]
    current = replace(stale, targets=targets)
    # Contract is accepted; absent acquisition target then takes the ordinary failure path.
    result = module.collect(SimpleNamespace(app_profile=current, base_context={}))
    assert result.reason.endswith('_target_invalid')


def test_content_windows_keep_boundary_observation_after_visiting():
    views = iter(['A','B','B'])
    moves=[]
    assert list(collection.iter_windows(lambda:next(views), lambda:moves.append('up'), lambda x:x,
                                       include_boundary=True, adjacent=True)) == ['A','B','B']
    assert moves == ['up','up']


@pytest.mark.parametrize('section,value', [('ui_fields', {'title': ''}), ('display_patterns', {'message_status':'['}), ('display_patterns', {'message_status':'no captures'})])
def test_invalid_parser_configuration_is_rejected_before_execution(tmp_path, section, value):
    import json
    from pathlib import Path
    from aura.profiles import ProfileStore, ProfileError
    app,version = ('chrome','156.0.8078.79') if section=='ui_fields' else ('telegram','13.1.1')
    doc=json.loads((Path(__file__).resolve().parents[1]/'profiles/apps'/app/(version+'.json')).read_text())
    doc['parameters'][section].update(value)
    path=tmp_path/'apps'/app/(version+'.json'); path.parent.mkdir(parents=True);path.write_text(json.dumps(doc))
    with pytest.raises(ProfileError, match=section):
        ProfileStore(tmp_path).load_app(app,version)


def test_visit_orders_entry_collection_and_verified_return():
    calls=[]
    assert collection.visit_item(lambda:calls.append('open'), lambda:(calls.append('read'),42)[1],
                                 lambda:(calls.append('back'), 'parent')[1]) == (42,'parent')
    assert calls == ['open','read','back']


def test_adjacent_boundary_requires_consecutive_confirmations():
    views = iter(['A', 'B', 'B', 'C', 'C', 'C', 'C', 'UNREACHED'])
    assert list(collection.iter_windows(lambda: next(views), lambda: None, str,
        include_boundary=True, adjacent=True, boundary_repeats=3)) == ['A', 'B', 'B', 'C', 'C', 'C', 'C']
