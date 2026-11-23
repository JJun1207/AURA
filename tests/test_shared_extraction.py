import xml.etree.ElementTree as ET
from dataclasses import replace
import pytest

from aura import parsing
from fakes import browser_profile


def test_values_keep_occurrences_and_only_filter_explicitly():
    nodes = list(ET.fromstring('<r><n text=" Same "/><n text="Same"/><n text="Restore"/><n/><n text="Other"/></r>'))
    assert parsing.node_values(nodes) == ['Same', 'Same', 'Restore', 'Other']
    assert parsing.node_values(nodes, exclude={'Restore'}, unique=True) == ['Same', 'Other']
    assert parsing.node_values(nodes, prefix='Sam') == ['Same', 'Same']


def test_geometry_keeps_touching_edges_distinct_from_overlap():
    assert parsing.contains_bounds((0, 0, 10, 10), (0, 2, 10, 8))
    assert not parsing.contains_bounds((0, 0, 10, 10), (-1, 2, 10, 8))
    assert not parsing.overlaps_bounds((0, 0, 10, 10), (10, 0, 20, 10))
    assert parsing.overlaps_bounds((0, 0, 10, 10), (9, 0, 20, 10))


def test_drive_labels_are_profile_data_and_modified_value_still_follows_label():
    from aura.apps.google_drive.collector import parse_listing
    profile = browser_profile('google_drive', '2.26.437.0.all.alldpi')
    profile = replace(profile, parameters={**profile.parameters, 'display_text': {
        **profile.parameters.get('display_text', {}), 'more_actions_prefix': 'Menu: ',
        'folder_type': 'Directory', 'modified_label': 'Updated'}})
    xml = '<r><n clickable="true" bounds="[0,0][100,50]"><n content-desc="Directory"/><n text="Updated"/><n text="Yesterday"/><n resource-id="more_actions_button" bounds="[80,0][100,50]"><n content-desc="Menu: Same"/></n></n></r>'
    rows = parse_listing(xml, profile=profile)
    assert [(r.name, r.kind, r.displayed_modified) for r in rows] == [('Same', 'folder', 'Yesterday')]


def test_notes_history_row_and_actions_follow_profile():
    from copy import deepcopy
    from aura.apps.notesnook.collector import _history_rows, _history_version
    profile = browser_profile('notesnook', '3.4.15')
    parameters = deepcopy(profile.parameters)
    parameters.setdefault('list_parsing', {})['history'] = {
        'row': {'class': 'custom.HistoryRow', 'clickable': 'true'},
        'fields': {'label': {'attribute': 'content-desc'}}}
    parameters['display_patterns']['history_label'] = 'Version [0-9]+'
    parameters['display_text'].update(history_restore='Restore version', history_delete='Delete version')
    profile = replace(profile, parameters=parameters)
    root = ET.fromstring('<r><n package="com.streetwriters.notesnook" visible-to-user="true" class="custom.HistoryRow" clickable="true" content-desc="Version 1" bounds="[0,0][100,40]"/></r>')
    assert [r['label'] for r in _history_rows(root, profile)] == ['Version 1']
    root = ET.fromstring('<r><n package="com.streetwriters.notesnook" visible-to-user="true" class="android.widget.TextView" text="Title"/><n package="com.streetwriters.notesnook" visible-to-user="true" clickable="true" class="android.widget.TextView" text="Restore version" content-desc="Restore version"/></r>')
    record = _history_version(root, profile)
    assert record['visible_content'] == []
    assert record['restore_control_observed'] is True


def test_notion_page_title_selection_follows_profile_without_changing_page_identity():
    from copy import deepcopy
    from aura.apps.notion.pages import parse_home_rows
    profile = browser_profile('notion', '0.6.4280')
    parameters = deepcopy(profile.parameters)
    parameters['list_parsing']['page_titles']['row']['class'] = 'custom.PageTitle'
    profile = replace(profile, parameters=parameters)
    root = ET.fromstring('<r><n package="notion.id" visible-to-user="true" resource-id="home-tab.private.page-row.11111111-1111-1111-8111-111111111111" bounds="[0,0][100,100]"><n package="notion.id" visible-to-user="true" resource-id="home-tab.private.page-row.expand" bounds="[0,10][20,40]"/><n package="notion.id" visible-to-user="true" class="custom.PageTitle" text="Title" bounds="[20,10][100,40]"/></n></r>')
    rows = parse_home_rows(root, profile)
    assert [(r['page_ref'], r['title']) for r in rows] == [('page-11111111-1111-1111-8111-111111111111', 'Title')]


def test_history_label_uses_profile_field_for_output_and_overlap():
    from copy import deepcopy
    from aura.apps.notesnook.collector import _history_rows, _history_overlap
    profile = browser_profile('notesnook', '3.4.15')
    parameters = deepcopy(profile.parameters)
    parameters['list_parsing']['history']['fields']['label'] = {'attribute': 'text'}
    parameters['display_patterns']['history_label'] = 'Version [0-9]+'
    profile = replace(profile, parameters=parameters)
    def rows(labels):
        root = ET.fromstring('<r>' + ''.join(
            f'<n package="com.streetwriters.notesnook" visible-to-user="true" class="android.view.ViewGroup" clickable="true" text="Version {label}" content-desc="unrelated" bounds="[0,{i*50}][100,{i*50+40}]"/>'
            for i, label in enumerate(labels)) + '</r>')
        return _history_rows(root, profile)
    before, after = rows([1, 2]), rows([2, 3])
    assert [r['label'] for r in before] == ['Version 1', 'Version 2']
    assert _history_overlap(before, after) == 1


@pytest.mark.parametrize('app,version', [('notesnook', '3.4.15'), ('notion', '0.6.4280')])
def test_missing_row_rules_fail_profile_validation_before_acquisition(app, version):
    from aura.profiles import ProfileStore, ProfileError
    parameters = {k:v for k,v in browser_profile(app, version).parameters.items() if k != 'list_parsing'}
    with pytest.raises(ProfileError, match='list_parsing'):
        ProfileStore._validate_parser_parameters(app, parameters)
