"""Samsung Browser UI record parsers."""

from __future__ import annotations
from ...collection import (
    collect_record_list,
    field_id,
    content_signature as _browser_signature,
    retain_ui_observation,
    merge_history_windows,
    collect_record_windows,
    window_merge_metadata,
    ui_selector,
    display_text,
    poll_samples,
    first_stable,
)

from ...parsing import bind_parsing_contracts, parsing_spec, read_rows, field_value, matches_node
from ...geometry import contains_bounds, parse_bounds

from functools import partial
import time
import xml.etree.ElementTree as ET


from ...models import (
    AcquisitionStatus,
    ActionRef,
    Outcome,
    OutcomeStatus,
)


PREFIX = "com.sec.android.app.sbrowser:id/"


def _value(node: ET.Element, resource_id: str) -> str:
    return field_value(node, resource_id, resource_prefix=PREFIX, ui_fields={})


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _selector(context, name):
    try:
        return ui_selector(context.app_profile, name)
    except (KeyError, TypeError):
        raise RuntimeError(f"invalid Samsung Browser selector: {name}") from None


def _parsed_rows(xml, view, profile):
    return read_rows(_root(xml), parsing_spec(profile, view), ui_fields=profile.parameters['ui_fields'], resource_prefix=PREFIX)


def parse_history(xml: str, *, profile) -> list[dict[str, str]]:
    spec = parsing_spec(profile, 'history')
    root = _root(xml)
    groups = {node: group['label'] for group, node in _history_group_nodes(root, profile)}
    return [record for record, _ in read_rows(root, spec,
        ui_fields=profile.parameters['ui_fields'], resource_prefix=PREFIX, group_values=groups)]


def _history_groups(context, xml):
    return [group for group, _ in _history_group_nodes(_root(xml), context.app_profile)]


def _history_group_nodes(root, profile):
    """Read identity and expansion state from the same owning header row."""
    parents = {child: parent for parent in root.iter() for child in parent}
    states = {display_text(profile, 'history_expand'): False,
              display_text(profile, 'history_collapse'): True}
    header_states = {False: display_text(profile, 'history_header_collapsed'),
                     True: display_text(profile, 'history_header_expanded')}
    groups = []
    for node in root.iter():
        if (not matches_node(node, {'package': profile.package_name, 'resource_id': 'group_header_indicator'}, ui_fields=profile.parameters['ui_fields'], resource_prefix=PREFIX)
                or node.get("visible-to-user") == "false"):
            continue
        description = node.get("content-desc", "")
        indicator_label = ''
        if description in states:
            expanded = states[description]
        else:
            state_text, separator, indicator_label = description.partition(',')
            indicator_label = indicator_label.strip()
            current_states = {value: state for state, value in header_states.items()}
            if not separator or not indicator_label or state_text.strip() not in current_states:
                raise RuntimeError("samsung_browser_history_group_state_unavailable")
            expanded = current_states[state_text.strip()]
        header = parents[node]
        text_label = _value(header, field_id(profile, "title"))
        label, header_description = text_label, ''
        ancestor = header
        while ancestor is not None and ancestor.get('scrollable') != 'true':
            if ancestor.get('clickable') == 'true':
                # Stop at the first owning row, never borrow a neighbouring
                # header's identity from a list/container ancestor.
                if (ancestor.get('package') != profile.package_name
                        or sum(n.get('resource-id') == node.get('resource-id') for n in ancestor.iter()) != 1):
                    raise RuntimeError('samsung_browser_history_group_owner_ambiguous')
                header = ancestor
                header_description = ancestor.get('content-desc', '').strip()
                if header_description:
                    header_state, separator, header_label = header_description.partition(',')
                    header_label = header_label.strip()
                    # Samsung 28 updates the indicator but leaves its parent's
                    # expansion word stale after a toggle. Keep label checks.
                    if (not separator or header_state.strip() not in header_states.values() or not header_label
                            or (not indicator_label and header_state.strip() != header_states[expanded])
                            or (text_label and text_label != header_label)):
                        raise RuntimeError('samsung_browser_history_group_identity_conflict')
                    label = header_label
                break
            ancestor = parents.get(ancestor)
        if indicator_label and label != indicator_label:
            raise RuntimeError('samsung_browser_history_group_identity_conflict')
        if not label or any(g['label'] == label for g, _ in groups):
            raise RuntimeError('samsung_browser_history_group_state_unavailable')
        groups.append(({'label': label, 'expanded': expanded,
                        'indicator_description': description,
                        'bounds': parse_bounds(node.get('bounds', '')),
                        'label_source': 'header_text' if text_label else 'header_content_description',
                        'header_description': header_description}, header))
    return groups


def _read_history_window(context, attempt_id, state):
    """Expand headers before the shared collector tests its scroll boundary."""
    profile = context.app_profile
    hierarchy = context.device.hierarchy()
    while True:
        groups = _history_groups(context, hierarchy)
        resolved = state.setdefault('description_label_groups', set())
        for group in groups:
            if group['label_source'] == 'header_content_description' and group['label'] not in resolved:
                state['action'] = context.linked_action('confirm_samsung_browser_history_group_from_description',
                    attempt_id, source_action=state['action'],
                    context={'surface': 'history', 'date_group': group['label']}, details=group)
                resolved.add(group['label'])
        collapsed = [g for g in groups if not g["expanded"]]
        if not collapsed:
            return hierarchy
        label = collapsed[0]["label"]
        pending = state.setdefault("deferred_date_groups", set())
        area = context.device.interaction_bounds()
        if not contains_bounds(area, collapsed[0]["bounds"]):
            pending.add(label)
            state["action"] = context.linked_action("defer_samsung_browser_history_group",
                attempt_id, source_action=state["action"],
                context={"surface": "history", "date_group": label},
                details={"bounds": collapsed[0]["bounds"], "interaction_bounds": area})
            # The normal window collector preserves and merges these rows before
            # scrolling. Scrolling here would skip the uncollected current window.
            return hierarchy
        pending.discard(label)
        if label in state["expanded_date_groups"]:
            raise RuntimeError(f"samsung_browser_history_group_recollapsed: {label}")
        number = len(state["expanded_date_groups"]) + 1
        linked = {"surface": "history", "date_group": label}

        def retain(phase, xml):
            state["action"], _ = retain_ui_observation(
                context, attempt_id, f"samsung-browser/history/groups/{number:04d}-{phase}", xml,
                name="observe_samsung_browser_history_group", source_action=state["action"],
                linked={**linked, "phase": phase})

        retain("before", hierarchy)
        # Select only the first visible collapsed indicator; other headers may
        # move off screen when this group opens, so reacquire before the next click.
        state["action"] = context.ui.click("expand_samsung_browser_history_group", {
            "resourceId": PREFIX + field_id(profile, "group_header_indicator"),
            "description": collapsed[0]['indicator_description'],
            "packageName": profile.package_name, "instance": 0,
        }, context={**linked, "source_action_id": state["action"].action_id})
        time.sleep(profile.timings["history_group_expand_settle"])

        def expanded_signature(xml):
            groups = _history_groups(context, xml)
            if any(g["label"] == label and g["expanded"] for g in groups):
                return _browser_signature(xml, profile.package_name)
            return None

        hierarchy = first_stable(poll_samples(context.device.hierarchy,
            timeout=profile.timings["history_group_expand_timeout"],
            interval=profile.timings["poll_interval"]), key=expanded_signature)
        if hierarchy is None:
            context.journal.record_action("verify_samsung_browser_history_group_expanded",
                status="failed", attempt_id=attempt_id,
                context={**linked, "source_action_id": state["action"].action_id})
            raise RuntimeError(f"samsung_browser_history_group_expansion_unverified: {label}")
        state["action"] = context.linked_action("verify_samsung_browser_history_group_expanded",
            attempt_id, source_action=state["action"], context=linked)
        state["expanded_date_groups"].append(label)
        # The next group's before capture or the list capture retains this
        # verified expanded XML, linked to the verification action above.


def parse_downloads(xml: str, *, profile) -> list[dict[str, str]]:
    return [record for record, _ in _parsed_rows(xml, "downloads", profile)]


def parse_saved_pages(xml: str, *, profile) -> list[dict[str, str]]:
    return [record for record, _ in _parsed_rows(xml, "saved_pages", profile)]


def parse_bookmarks(xml: str, *, profile) -> list[dict[str, str]]:
    return [record for record, _ in _parsed_rows(xml, "bookmarks", profile)]


def _return_to_main(context) -> ActionRef | None:
    main = _selector(context, "samsung_browser.main")
    if context.device.exists(main):
        return None
    seen = set()
    action = None
    while not context.device.exists(main):
        hierarchy = context.device.hierarchy()
        signature = _browser_signature(hierarchy, "com.sec.android.app.sbrowser")
        if signature in seen:
            raise RuntimeError("Samsung Browser main screen is unavailable")
        seen.add(signature)
        action = context.ui.back("return_to_samsung_browser_main")
        time.sleep(context.app_profile.timings["transition_settle"])
    assert action is not None
    return action


def _open_surface(context, menu_item: str, marker: str) -> ActionRef:
    _return_to_main(context)
    settle = context.app_profile.timings["transition_settle"]
    context.ui.click(
        "open_samsung_browser_menu",
        _selector(context, "samsung_browser.menu"),
        expected=_selector(context, "samsung_browser.menu.list"),
    )
    time.sleep(settle)
    action = context.ui.click(
        f"open_{menu_item.replace('.', '_')}",
        _selector(context, menu_item),
        expected=_selector(context, marker),
    )
    time.sleep(settle)
    return action


def _open_bookmarks(context) -> ActionRef:
    _return_to_main(context)
    action = context.ui.click(
        "open_samsung_browser_bookmarks",
        _selector(context, "samsung_browser.bookmarks"),
        expected=_selector(context, "samsung_browser.screen.bookmarks"),
    )
    time.sleep(context.app_profile.timings["transition_settle"])
    return action


def _collect_list(context, *, surface: str, target_id: str, item_type: str, parser, opener) -> tuple[int, ActionRef]:
    def collect(attempt_id, state):
        collect_record_windows(context, attempt_id, state, surface=surface, prefix=f'samsung-browser/{surface}',
            observe_name='observe_samsung_browser_ui', scroll_name=f'scroll_samsung_browser_{surface}', parser=parser,
            carry_groups=surface == 'history',
            settle=context.app_profile.timings['scroll_settle'],
            read=partial(_read_history_window, context, attempt_id, state) if surface == 'history' else None,
            overlap_error=f"samsung_browser_{surface.replace('-', '_')}_window_overlap_unconfirmed")
        if state.get('deferred_date_groups'):
            raise RuntimeError('samsung_browser_history_group_outside_interaction_area: '
                               + ', '.join(sorted(state['deferred_date_groups'])))
    def metadata(state):
        result = {'expanded_date_groups': state['expanded_date_groups'],
                  'deferred_date_groups': sorted(state.get('deferred_date_groups', [])),
                  'description_label_groups': sorted(state.get('description_label_groups', []))} if surface == 'history' else {}
        return {**result, **window_merge_metadata(state)}
    return collect_record_list(context, target_id=target_id, item_key=f'samsung-browser:{surface}', item_type=item_type,
        surface=surface, prefix=f'samsung-browser/{surface}', record_kind=f'samsung_browser_{surface}',
        retain_name='retain_samsung_browser_records',
        completion='repeated_hierarchy_with_expanded_date_groups' if surface == 'history' else 'repeated_hierarchy',
        opener=opener, collect=collect, initial_state={'expanded_date_groups': []}, metadata=metadata)


def identification_contracts(profile):
    contracts = {
        'samsung_browser.history': ('samsung_browser.parse_history', {'row_resource_id': 'history_relative', 'required_child_resource_id': 'history_title', 'identity_fields': ['date_group', 'title', 'displayed_address', 'displayed_time']}),
        'samsung_browser.downloads': ('samsung_browser.parse_downloads', {'row_resource_id': 'download_item_parent', 'required_child_resource_id': 'download_item_title'}),
        'samsung_browser.saved_pages': ('samsung_browser.parse_saved_pages', {'row_resource_id': 'saved_page_list_view_layout', 'required_child_resource_id': 'saved_page_list_view_title_text_view'}),
        'samsung_browser.bookmarks': ('samsung_browser.parse_bookmarks', {'row_resource_id': 'displayed_view', 'required_child_resource_id': 'bookmark_folder_title'}),
    }
    contracts["samsung_browser.history"][1]["window_merge"] = "longest_adjacent_ordered_record_overlap"
    contracts["samsung_browser.history"][1].update(
        group_indicator_resource_id="group_header_indicator",
        collapsed_description=display_text(profile, 'history_expand'),
        expanded_description=display_text(profile, 'history_collapse'),
    )
    return bind_parsing_contracts(profile, contracts)


def collect(context) -> Outcome:
    context.app_profile.require_identification_rules(identification_contracts(context.app_profile))
    condition = context.base_context.get("condition", {})
    target = condition.get("target") if isinstance(condition, dict) else None
    if (
        not isinstance(target, dict)
        or target.get("kind") != "browser"
        or target.get("ref") != "browser.samsung-browser.all"
    ):
        return Outcome(OutcomeStatus.FAILED, "samsung_browser_target_invalid")
    if not context.start_app():
        return Outcome(
            OutcomeStatus.FAILED, "samsung_browser_app_start_not_verified"
        )

    specifications = (
        (
            "history",
            "samsung_browser.history",
            "history_collection",
            partial(parse_history, profile=context.app_profile),
            lambda: _open_surface(
                context,
                "samsung_browser.menu.history",
                "samsung_browser.screen.history",
            ),
        ),
        (
            "downloads",
            "samsung_browser.downloads",
            "download_collection",
            partial(parse_downloads, profile=context.app_profile),
            lambda: _open_surface(
                context,
                "samsung_browser.menu.downloads",
                "samsung_browser.screen.downloads",
            ),
        ),
        (
            "saved-pages",
            "samsung_browser.saved_pages",
            "saved_page_collection",
            partial(parse_saved_pages, profile=context.app_profile),
            lambda: _open_surface(
                context,
                "samsung_browser.menu.saved-pages",
                "samsung_browser.screen.saved-pages",
            ),
        ),
        (
            "bookmarks",
            "samsung_browser.bookmarks",
            "bookmark_collection",
            partial(parse_bookmarks, profile=context.app_profile),
            lambda: _open_bookmarks(context),
        ),
    )
    counts = {}
    for surface, target_id, item_type, parser, opener in specifications:
        counts[surface], action = _collect_list(
            context,
            surface=surface,
            target_id=target_id,
            item_type=item_type,
            parser=parser,
            opener=opener,
        )
    incomplete_reason = next((a.reason for a in context.attempts
                              if a.acquisition_status == AcquisitionStatus.PARTIAL), None)
    return Outcome(
        OutcomeStatus.PARTIAL if incomplete_reason else OutcomeStatus.COMPLETE,
        reason=incomplete_reason,
        action=action,
        context={"target_ref": target["ref"]},
        details={"record_counts": counts},
    )


__all__ = [
    "parse_bookmarks",
    "parse_downloads",
    "parse_history",
    "parse_saved_pages",
    "collect",
]
