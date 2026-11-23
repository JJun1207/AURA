"""Contracts at shared algorithm boundaries, independent of device UI backends."""
from pathlib import PurePosixPath
import pytest
from aura import artifacts, parsing


def test_inventory_formats_preserve_paths_and_root_policy():
    parse = getattr(artifacts, 'parse_inventory', None)
    assert callable(parse), 'shared inventory decoding is missing'
    roots = (PurePosixPath('/sdcard/Download'),)
    for data, fmt in [('/sdcard/Download/a\tb\t2\t3\n', 'tab_lines'),
                      ('/sdcard/Download/a\tb\x002\x003\x00', 'nul_triples')]:
        assert parse(data, roots, format=fmt) == (('/sdcard/Download/a\tb', 2, 3),)
        assert parse('', roots, format=fmt) == ()
    assert parse('/sdcard/Download\x000\x000\x00', roots, format='nul_triples') == (('/sdcard/Download', 0, 0),)
    with pytest.raises(ValueError):
        parse('/sdcard/Download\t0\t0', roots, format='tab_lines', allow_root=False)
    for bad in ['/sdcard/Download/x\x001\x002', '/sdcard/Download/x\x001\x00',
                '/elsewhere/x\x001\x002\x00', '/sdcard/Download/../x\x001\x002\x00',
                '/sdcard/Download/x\x00-1\x002\x00', '/sdcard/Download/x\x00a\x002\x00',
                '/sdcard/Download/x\x001\x002\x00' * 2]:
        with pytest.raises(ValueError):
            parse(bad, roots, format='nul_triples')


def test_bounds_policies_do_not_invent_visibility():
    parse = getattr(parsing, 'parse_bounds', None)
    assert callable(parse), 'shared bounds decoding is missing'
    assert parse('[-1,2][3,4]') == (-1, 2, 3, 4)
    assert parse('[3,2][3,4]', positive_area=False) == (3, 2, 3, 4)
    for value, options in [('oops', {}), ('[3,2][3,4]', {}), ('[-1,2][3,4]', {'allow_negative': False})]:
        with pytest.raises(ValueError):
            parse(value, **options)



def test_selector_translation_keeps_boolean_and_allowed_key_boundaries():
    from aura import collection
    convert = getattr(collection, 'selector_conditions', None)
    assert callable(convert), 'shared selector conversion is missing'
    values = {'resource_id': 'app:id/title', 'checked': False, 'text': ' A '}
    assert convert(values, backend='xml') == {'resource-id': 'app:id/title', 'checked': 'false', 'text': ' A '}
    assert convert(values, backend='uiautomator') == {'resourceId': 'app:id/title', 'checked': False, 'text': ' A '}
    with pytest.raises(KeyError):
        convert(values, backend='xml', allowed={'text', 'checked'})
    one = getattr(collection, 'one_candidate', None)
    assert callable(one), 'candidate cardinality is missing'
    assert one(iter(['one'])) == 'one'
    with pytest.raises(ValueError, match='ambiguous'):
        one(['same', 'same'])
    with pytest.raises(ValueError, match='unavailable'):
        one([])


def test_field_scope_and_attribute_fallback_stay_on_first_node():
    import xml.etree.ElementTree as E
    root = E.fromstring('<row><outer><v resource-id="label" text="nested"/></outer><v resource-id="label" text="" content-desc="direct"/><v resource-id="label" text="later"/></row>')
    source = {'resource_id': 'label', 'scope': 'children', 'attribute': ['text', 'content-desc']}
    assert parsing.field_value(root, source, ui_fields={}) == 'direct'
    assert parsing.field_value(root, {'resource_id': 'label'}, ui_fields={}) == 'nested'
    labels = getattr(parsing, 'labelled_values', None)
    assert callable(labels), 'shared label extraction is missing'
    assert labels(['Title', 'Created', 'today', 'Edited', 'now'], {'created': 'Created', 'edited': 'Edited'}) == ({'created': 'today', 'edited': 'now'}, {'created': 1, 'edited': 3})
    with pytest.raises(ValueError):
        labels(['Title', 'Created'], {'created': 'Created'})


def test_profile_labels_and_direct_fields_are_consumed():
    from copy import deepcopy
    from fakes import browser_profile
    from aura.apps.notesnook import collector as notes
    from aura.apps.google_drive import collector as drive
    import xml.etree.ElementTree as E
    profile = deepcopy(browser_profile('notesnook', '3.4.15'))
    profile.parameters['display_text'].update(created_at='Made', last_edited_at='Edited')
    root = E.fromstring('<hierarchy>' + ''.join(f'<node package="com.streetwriters.notesnook" visible-to-user="true" class="android.widget.TextView" text="{s}"/>' for s in ['Title','Made','yesterday','Edited','today']) + '</hierarchy>')
    assert notes._note_menu_record(root, profile) == {'title':'Title','created_at':'yesterday','last_edited_at':'today'}
    profile = deepcopy(browser_profile('google_drive', '2.26.437.0.all.alldpi'))
    profile.parameters['display_text']['activity_end_prefix'] = 'End '
    assert drive.activity_complete('End yesterday', profile)
    assert not drive.activity_complete('No activity recorded before yesterday', profile)



def test_stability_probe_resets_only_as_requested_and_propagates_cancel():
    from aura import collection
    make = getattr(collection, 'stability_probe', None)
    assert callable(make), 'shared stabilization state is missing'
    for reset, want in [(True, [False, False, False, True]), (False, [False, False, True, True])]:
        samples = iter(['A', 'bad', 'A', 'A'])
        def key(value):
            if value == 'bad': raise ValueError('transient')
            return value
        probe, state = make(lambda: next(samples), key=key, retry_errors=(ValueError,), reset_on_error=reset)
        assert [probe() for _ in range(4)] == want
        assert state['value'] == 'A'
        assert str(state['error']) == 'transient'
    probe, _ = make(lambda: 'bad', key=key)
    with pytest.raises(ValueError): probe()
    def cancel(value): raise KeyboardInterrupt()
    probe, _ = make(lambda: 'A', key=cancel, retry_errors=(Exception,))
    with pytest.raises(KeyboardInterrupt): probe()


def test_file_candidate_priority_and_pull_preserve_real_bytes(tmp_path):
    choose = getattr(artifacts, 'select_inventory_file', None)
    assert callable(choose), 'shared file candidate selection is missing'
    original = ('/r/a.jpg', 3, 1)
    changed = ('/r/b.jpg', 4, 2)
    assert choose((original,), (original, changed)) == (changed, True)
    assert choose((original,), (original,), allow_existing=True) == (original, False)
    with pytest.raises(ValueError): choose((), (original, changed), allow_existing=True)
    with pytest.raises(ValueError): choose((original,), (original,))
    class Device:
        def pull(self, remote, local):
            assert remote == '/r/a.jpg'
            assert local.name == 'image'
            local.write_bytes(b'abc')
    assert artifacts.pull_bytes(Device(), '/r/a.jpg', filename='image', directory=tmp_path) == b'abc'
    assert list(tmp_path.iterdir()) == []


def test_overlap_reconciliation_keeps_occurrences_and_mutation_policy():
    from aura import collection
    reconcile = getattr(collection, 'reconcile_overlap', None)
    anchor = getattr(collection, 'unique_overlap_anchor', None)
    assert callable(reconcile) and callable(anchor), 'shared overlap reconciliation is missing'
    older = [{'id': 'repeat'}, {'id': 'repeat'}, {'id': 'anchor', 'sender': 'A'}]
    newer = [{'id': 'anchor'}, {'id': 'next'}]
    same = lambda a, b: a['id'] == b['id']
    assert anchor(older, newer, 1, same=same)
    assert not anchor([{'id':'x'}, {'id':'x'}], [{'id':'x'}, {'id':'x'}], 2, same=same)
    result = reconcile(older, newer, 1, lambda old, new: {**new, **old}, copy_newer=dict)
    assert result == [{'id':'repeat'}, {'id':'repeat'}, {'id':'anchor','sender':'A'}, {'id':'next'}]
    assert newer == [{'id':'anchor'}, {'id':'next'}]
    assert result[-1] is not newer[-1]
    def mutate(old, new):
        new.update(old)
        return new
    result = reconcile(older, newer, 1, mutate)
    assert newer[0]['sender'] == 'A' and result[-1] is newer[-1]


def test_seek_checks_boundary_window_and_retains_last_observation():
    from aura import collection
    seek = getattr(collection, 'seek_window', None)
    assert callable(seek), 'shared window search is missing'
    for adjacent, expected_calls in [(False, ['A','B','A']), (True, ['A','B','A','A'])]:
        source = iter(['A','B','A','A']); calls = []; moved = []
        def observe():
            value = next(source); calls.append(value); return value
        found, last = seek(observe, lambda: moved.append('move'), lambda v:v,
                           lambda v: None, adjacent=adjacent)
        assert found is None and last == 'A' and calls == expected_calls
        assert len(moved) == len(calls)-1
    source = iter([('A',False),('A',True)])
    assert seek(lambda:next(source), lambda:None, lambda v:v[0], lambda v:'found' if v[1] else None) == ('found', ('A',True))



@pytest.mark.parametrize('failure', [None, RuntimeError('stopped'), KeyboardInterrupt()])
def test_list_lifecycle_retains_partial_rows_and_original_failure(tmp_path, failure):
    import json
    from aura import collection
    from aura.artifacts import ArtifactStore
    from aura.journal import EventJournal
    from aura.profiles import ProfileStore
    from aura.runtime import RunContext
    from fakes import FakeDevice
    run = getattr(collection, 'collect_record_list', None)
    assert callable(run), 'shared list lifecycle is missing'
    store = ProfileStore(__import__('pathlib').Path(__file__).parents[1] / 'profiles')
    journal = EventJournal(tmp_path / 'events.jsonl')
    context = RunContext(run_dir=tmp_path, base_context={}, device=FakeDevice(), journal=journal,
        artifacts=ArtifactStore(tmp_path, journal), app_profile=store.load_app('chrome','156.0.8078.79'),
        system_ui_profile=store.load_system_ui('samsung'), ui=None)
    def collect(attempt, state):
        state['action'], _ = collection.retain_ui_observation(context, attempt, 'chrome/downloads/window-0001', '<hierarchy/>',
            name='observe_chrome_ui', source_action=state['action'], linked={'surface':'downloads', 'window':1})
        state['records'].extend([{'title':'same'}, {'title':'same'}])
        if failure is not None: raise failure
    def invoke():
        return run(context, target_id='chrome.downloads', item_key='chrome:downloads', item_type='download_collection',
            surface='downloads', prefix='chrome/downloads', record_kind='chrome_downloads', retain_name='retain_chrome_records',
            completion='repeated_hierarchy', opener=lambda:journal.record_action('open_downloads',status='success'), collect=collect)
    if failure is not None:
        with pytest.raises(type(failure)) as caught: invoke()
        assert caught.value is failure
    else:
        assert invoke()[0] == 2
    saved = json.loads((tmp_path/'artifacts/chrome/downloads.json').read_text())
    assert saved['records'] == [{'title':'same'}, {'title':'same'}]
    assert context.attempts[0].reason == (str(failure) or type(failure).__name__ if failure is not None else None)
    context.validate_records()


def test_snapshot_buffer_retains_supplied_evidence_and_source_event(tmp_path):
    import json
    from aura import runtime
    from aura.journal import EventJournal
    record = getattr(runtime, 'record_snapshot', None)
    assert callable(record), 'shared snapshot registration is missing'
    journal = EventJournal(tmp_path/'events.jsonl')
    action = journal.record_action('open', status='success')
    screen, tree = b'preserved png', b'<hierarchy/>'
    assert record(journal, 'snapshot-1', action, screen, tree) == {'screen_image':screen, 'ui_tree':tree}
    event = json.loads(journal.path.read_text().splitlines()[-1])
    assert event['context'] == {'observation_id':'snapshot-1'}
    assert event['details'] == {'action_event_id':action.event_id}


def test_validated_own_row_attribute_fallback_can_be_executed():
    import xml.etree.ElementTree as E
    views = {'own': {'row': {}, 'fields': {'label': {'attribute': ['text','content-desc']}}}}
    parsing.validate_list_parsing(views, {'own': {'fields': ('label',)}}, package_name='app')
    values = parsing.read_rows(E.fromstring('<node text="" content-desc="fallback"/>'), views['own'], ui_fields={})
    assert values[0][0] == {'label':'fallback'}
