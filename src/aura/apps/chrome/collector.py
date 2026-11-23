"""Chrome UI record parsers."""

from __future__ import annotations
from ...collection import (
    collect_record_list,
    stability_probe,
    ordered_candidate,
    visit_scope,
    field_id,
    content_signature,
    window_overlap as _window_overlap,
    merge_history_windows,
    retain_ui_observation,
    finish_record_collection,
    collect_record_windows,
    ui_selector,
    selector_conditions,
    iter_windows,
    walk_hierarchy,
)


from ...parsing import (bind_parsing_contracts, parsing_spec, read_rows, read_sections,
                       bind_row_actions, first_node, field_value)

import json
from functools import partial
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable

from ...models import (
    AcquisitionStatus,
    ActionRef,
    Outcome,
    OutcomeStatus,
)
from ...ui import UiTimeout


PREFIX = "com.android.chrome:id/"


def merge_windows(
    collected: list[dict[str, object]],
    visible: list[dict[str, object]],
) -> list[dict[str, object]]:
    return merge_history_windows(collected, visible)[0]


def merge_download_windows(
    collected: list[dict[str, object]], visible: list[dict[str, object]]
) -> list[dict[str, object]]:
    return merge_windows(collected, visible)



def _text(node: ET.Element) -> str:
    return node.get("text", "").strip()


def _descendant(node: ET.Element, resource_id: str) -> ET.Element | None:
    return first_node(node, {'resource_id': resource_id}, suffix=True, ui_fields={})


def _value(node: ET.Element, resource_id: str) -> str:
    return field_value(node, resource_id, suffix=True, ui_fields={})


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _browser_signature(xml, package_name="com.android.chrome"):
    return content_signature(xml, package_name)


def _bookmark_title(xml: str, profile) -> str:
    root = _root(xml)
    action_bar = first_node(root, {'resource_id': 'action_bar'}, ui_fields=profile.parameters['ui_fields'], suffix=True)
    if action_bar is None:
        return ""
    return next(
        (
            _text(node)
            for node in action_bar.iter()
            if node.get("class") == "android.widget.TextView" and _text(node)
        ),
        "",
    )


def wait_for_bookmark_title(context, current: str, *, expected: str | None = None, previous_xml: str | None = None) -> str:
    time.sleep(context.app_profile.timings['transition_settle'])
    previous_content = (_bookmark_content(parse_bookmarks(previous_xml, folder_path=(), profile=context.app_profile))
                        if previous_xml is not None else None)
    def read():
        try:
            hierarchy = context.device.hierarchy()
            return hierarchy, _bookmark_title(hierarchy, profile=context.app_profile)
        except ET.ParseError:
            return None
    def key(value):
        if value is None:
            return None
        hierarchy, title = value
        title_matches = title == expected if expected is not None else bool(title)
        content = _bookmark_content(parse_bookmarks(hierarchy, folder_path=(), profile=context.app_profile))
        content_changed = previous_content is not None and content != previous_content
        if not title_matches or not (title != current or content_changed):
            return None
        return json.dumps([title, content], sort_keys=True)
    transitioned, state = stability_probe(read, key=key)
    if not context.ui.wait_until(transitioned):
        raise RuntimeError('Chrome bookmark transition did not complete')
    return state['value'][0]


def _bookmark_back_visible(root, profile):
    toolbar = first_node(root, {'resource_id': 'action_bar'}, ui_fields=profile.parameters['ui_fields'], suffix=True)
    entry = profile.selectors['chrome.bookmarks.go-back']
    spec = entry['selector']
    attributes = selector_conditions({spec['kind']: spec['value'], **entry.get('constraints', {})}, backend='xml')
    return toolbar is not None and first_node(toolbar, attributes, ui_fields={}) is not None


def _bookmark_root_visible(xml: str, profile) -> bool:
    root = _root(xml)
    recycler = first_node(root, {'resource_id': 'selectable_list_recycler_view'}, ui_fields=profile.parameters['ui_fields'], suffix=True)
    return recycler is not None and not _bookmark_back_visible(root, profile) and any(
        child.get("clickable") != "true" and _value(child, field_id(profile, "title"))
        for child in recycler
    )


def _return_to_bookmark_root(context, action: ActionRef) -> tuple[ActionRef, str]:
    rewinding = False
    seen = set()
    hierarchy = context.device.hierarchy()
    while True:
        if _bookmark_root_visible(hierarchy, profile=context.app_profile) and not rewinding:
            return action, hierarchy
        toolbar = _descendant(_root(hierarchy), field_id(context.app_profile, "action_bar"))
        create = _descendant(toolbar, field_id(context.app_profile, "create_new_folder_menu_id")) if toolbar is not None else None
        if (create is not None and create.get("enabled") == "false"
                and not _bookmark_back_visible(_root(hierarchy), context.app_profile)):
            # Root can reopen at its remembered scroll offset with no section header.
            rewinding = True
            signature = _bookmark_signature(hierarchy, profile=context.app_profile)
            if signature in seen:
                return action, hierarchy
            seen.add(signature)
            action = context.ui.swipe("rewind_chrome_bookmark_root", "down", context={"surface": "bookmarks"})
            time.sleep(context.app_profile.timings["transition_settle"])
            hierarchy = context.device.hierarchy()
            continue
        current = _bookmark_title(hierarchy, profile=context.app_profile)
        if not current:
            raise RuntimeError("Chrome bookmark location is unavailable")
        action = context.ui.click(
            "return_to_chrome_bookmark_root",
            _selector(context, "chrome.bookmarks.go-back"),
            context={"surface": "bookmarks"},
        )
        hierarchy = wait_for_bookmark_title(context, current, previous_xml=hierarchy)


def _selector(context, name):
    try:
        return ui_selector(context.app_profile, name)
    except (KeyError, TypeError):
        raise RuntimeError(f"invalid Chrome selector: {name}") from None


def _parsed_rows(xml, view, profile):
    return read_rows(_root(xml), parsing_spec(profile, view), ui_fields=profile.parameters['ui_fields'], suffix=True)


def _parsed_bookmark_rows(xml, folder_path, *, root_section="", profile):
    root = _root(xml)
    path = list(folder_path)
    spec = parsing_spec(profile, "bookmarks")
    sections = read_sections(root, spec, initial=root_section, ui_fields=profile.parameters['ui_fields'], suffix=True) if not path else {}
    result = []
    for record, node in read_rows(root, spec, ui_fields=profile.parameters['ui_fields'], suffix=True):
        section = sections.get(node)
        record = {"kind": record["kind"], "folder_path": [*path, *([section] if section else [])],
                  **{key: value for key, value in record.items() if key != "kind"}}
        result.append((record, node))
    return result


def parse_history(xml: str, *, profile) -> list[dict[str, str]]:
    return [record for record, _ in _parsed_rows(xml, "history", profile)]


def parse_downloads(xml: str, *, profile) -> list[dict[str, str]]:
    return [record for record, _ in _parsed_rows(xml, "downloads", profile)]


def parse_bookmarks(
    xml: str, *, folder_path: Iterable[str], root_section: str = "", profile
) -> list[dict[str, object]]:
    return [record for record, _ in _parsed_bookmark_rows(
        xml, folder_path, root_section=root_section, profile=profile)]


def parse_recent_tabs(xml: str, *, profile) -> list[dict[str, str]]:
    return [record for record, _ in _parsed_rows(xml, "recent_tabs", profile)]


def parse_account(xml: str, *, profile) -> dict[str, str]:
    rows = _parsed_rows(xml, "account", profile)
    return rows[0][0] if rows else {}


def _parse_account_list(xml: str, *, profile) -> list[dict[str, str]]:
    record = parse_account(xml, profile=profile)
    return [record] if record else []


def _bookmark_rows(
    xml: str, folder_path: tuple[str, ...], *, root_section: str = "", profile
) -> list[tuple[dict[str, object], dict[str, object] | None]]:
    rows = _parsed_bookmark_rows(xml, folder_path, root_section=root_section, profile=profile)
    return bind_row_actions(rows, parsing_spec(profile, "bookmarks")["action"])


def _bookmark_content(records):
    # Chrome can recycle a folder's accessibility label onto an unrelated
    # bookmark row. Preserve the label as evidence, but not as row identity.
    return [{key: value for key, value in record.items() if key != "accessibility_description"}
            for record in records]


def _bookmark_signature(xml: str, *, profile) -> str:
    return json.dumps([_bookmark_title(xml, profile=profile),
                       _bookmark_content(parse_bookmarks(xml, folder_path=(), profile=profile))],
                      sort_keys=True)


def _open_surface(context, menu_item: str, marker: str) -> ActionRef:
    close = _selector(context, "chrome.close")
    menu = _selector(context, "chrome.menu")
    settle = context.app_profile.timings["transition_settle"]
    if context.device.exists(close):
        context.ui.click("close_chrome_surface", close, expected=menu)
        time.sleep(settle)
    elif context.device.exists(_selector(context, "chrome.screen.settings")):
        context.ui.back("leave_chrome_settings", expected=menu)
        time.sleep(settle)
    menu_list = _selector(context, "chrome.menu.list")
    try:
        context.ui.click("open_chrome_menu", menu, expected=menu_list)
    except UiTimeout:
        time.sleep(context.app_profile.timings["transition_settle"])
        context.ui.click(
            "open_chrome_menu_retry", menu, expected=menu_list
        )
    time.sleep(settle)
    action = context.ui.click(
        f"open_{menu_item.replace('.', '_')}",
        _selector(context, menu_item),
        expected=_selector(context, marker),
    )
    time.sleep(settle)
    return action


def _ready_history(context, attempt_id, source_action, *, allow_empty):
    """The empty-state view can precede loading; it alone is not completion."""
    hierarchy = ""
    polls = 0
    saw_loading = False
    empty_signature = None
    empty = False
    started = time.perf_counter()
    timeout = context.app_profile.timings["history_load_timeout"]

    def ready():
        nonlocal hierarchy, polls, saw_loading, empty_signature, empty
        hierarchy = context.device.hierarchy()
        polls += 1
        visible_ids = {node.get("resource-id") for node in _root(hierarchy).iter()
                       if node.get("visible-to-user") != "false"}
        loading = bool(visible_ids & {PREFIX + field_id(context.app_profile, name)
                                     for name in ("loading_view", "progress_spinner")})
        saw_loading = saw_loading or loading
        if parse_history(hierarchy, profile=context.app_profile) and not loading:
            return True
        empty_candidate = (allow_empty and saw_loading and not loading
                           and PREFIX + field_id(context.app_profile, "empty_state_text_title") in visible_ids)
        signature = _browser_signature(hierarchy) if empty_candidate else None
        empty = signature is not None and signature == empty_signature
        empty_signature = signature
        if polls == 1:
            print("AURA: Waiting for Chrome History to load.", flush=True)
        return empty

    completed = context.ui.wait_until(ready, timeout=timeout)
    if polls > 1 or not completed:
        source_action = context.journal.record_action(
            "wait_chrome_history_content", status="success" if completed else "failed",
            attempt_id=attempt_id, context={"source_action_id": source_action.action_id},
            details={"poll_count": polls, "timeout_seconds": timeout,
                     "elapsed_seconds": max(0.0, time.perf_counter() - started),
                     "loading_observed": saw_loading, "empty_after_loading": empty},
        )
    if not completed:
        raise UiTimeout("chrome_history_content_not_ready")
    return hierarchy, source_action, empty


def _collect_list(context, *, surface: str, target_id: str, item_type: str,
                  menu_item: str, marker: str, parser, scroll: bool = True) -> tuple[int, ActionRef]:
    def collect(attempt_id, state):
        def ready():
            xml, state['action'], state['empty'] = _ready_history(
                context, attempt_id, state['action'], allow_empty=not state['records'])
            return xml
        collect_record_windows(context, attempt_id, state, surface=surface, prefix=f'chrome/{surface}',
            observe_name='observe_chrome_ui', scroll_name=f'scroll_chrome_{surface}', parser=parser,
            settle=context.app_profile.timings['scroll_settle'],
            overlap_error=f"chrome_{surface.replace('-', '_')}_window_overlap_unconfirmed",
            read=ready if surface == 'history' else None, scroll=scroll)
    return collect_record_list(context, target_id=target_id, item_key=f'chrome:{surface}', item_type=item_type,
        surface=surface, prefix=f'chrome/{surface}', record_kind=f'chrome_{surface}', retain_name='retain_chrome_records',
        completion='repeated_hierarchy' if scroll else 'single_screen_parsed',
        opener=lambda: _open_surface(context, menu_item, marker), collect=collect)


def _collect_bookmarks(context) -> tuple[int, ActionRef]:
    surface = "bookmarks"
    context.begin_identification("chrome.bookmarks")
    attempt_id = context.item_attempt(
        "chrome:bookmarks",
        "chrome.bookmarks",
        "bookmark_collection",
        source={"surface": surface},
    )
    source_action = None
    records: list[dict[str, object]] = []
    window = 0

    def level(task):
        nonlocal window
        path, action, folder_ref, initial_xml = task
        previous = []
        entries = []
        entry_count = 0
        last_xml = initial_xml
        seen_windows = set()
        previous_signature = None
        repeated_navigation = 0.0
        navigation_started = None
        def read():
            nonlocal initial_xml, last_xml
            last_xml = initial_xml if initial_xml is not None else context.device.hierarchy()
            initial_xml = None
            return last_xml
        def advance():
            nonlocal action, navigation_started
            navigation_started = time.monotonic()
            action = context.ui.swipe(
                "scroll_chrome_bookmarks", "up",
                context={"folder_path": list(path), "window": window})
            time.sleep(context.app_profile.timings["scroll_settle"])
        def signature(xml):
            nonlocal previous_signature, repeated_navigation
            current = _bookmark_signature(xml, profile=context.app_profile)
            if current != previous_signature:
                if current in seen_windows:
                    # Only swipe/read time counts; captures and child visits may be slow.
                    repeated_navigation += time.monotonic() - navigation_started
                else:
                    seen_windows.add(current)
                    repeated_navigation = 0.0
            previous_signature = current
            return current
        for hierarchy in iter_windows(read, advance, signature, adjacent=True):
            parent_title = _bookmark_title(hierarchy, profile=context.app_profile)
            window += 1
            action, _ = retain_ui_observation(
                context, attempt_id, f"chrome/bookmarks/window-{window:04d}", hierarchy,
                name="observe_chrome_ui", source_action=action,
                linked={"surface": surface, "folder_path": list(path), "window": window},
            )
            rows = _bookmark_rows(hierarchy, path, profile=context.app_profile)
            visible = [record for record, _ in rows]
            overlap = _window_overlap(previous, visible, missing_keys=("folder_path",) if not path else ())
            root_section = ""
            if overlap and not path and previous[-overlap]["folder_path"]:
                # Inherit only through confirmed overlap; a visible header overrides it.
                root_section = previous[-overlap]["folder_path"][0]
                rows = _bookmark_rows(hierarchy, path, root_section=root_section, profile=context.app_profile)
                visible = [record for record, _ in rows]
            for index, record in enumerate(visible[:overlap], entry_count - overlap):
                entries[index].update({key: value for key, value in record.items()
                                       if value and not entries[index].get(key)})
            for entry_index, record in enumerate(visible[overlap:], entry_count):
                entry = {**record, "folder_ref": folder_ref, "entry_index": entry_index}
                entries.append(entry)
                records.append(entry)
            if previous and visible and not overlap:
                raise RuntimeError("chrome_bookmark_window_overlap_unconfirmed")
            if repeated_navigation and repeated_navigation >= context.app_profile.timings['default_timeout']:
                raise RuntimeError('chrome_bookmark_window_cycle_unresolved')
            current_xml = hierarchy
            for entry_index, (record, _) in enumerate(rows[overlap:], entry_count):
                if record["kind"] != "folder":
                    continue
                child_path = (*map(str, record["folder_path"]), str(record["title"]))
                # Returning from a child may reset the scroll position. Never reuse
                # an ordinal selector against a different parent window.
                def enter():
                    nonlocal action
                    current_rows = _bookmark_rows(current_xml, path, root_section=root_section, profile=context.app_profile)
                    try:
                        _, selector = ordered_candidate(
                            rows, current_rows, entry_index - entry_count + overlap,
                            key=lambda item: _bookmark_content([item[0]])[0])
                    except ValueError:
                        raise RuntimeError("chrome_bookmark_parent_window_changed") from None
                    if selector is None:
                        raise RuntimeError("Chrome bookmark selector is unavailable")
                    action = context.ui.click(
                        "open_chrome_bookmark_folder",
                        selector,
                        context={"folder_path": list(child_path), "folder_ref": f"{folder_ref}/{entry_index}"},
                    )
                    child_xml = wait_for_bookmark_title(
                        context, parent_title, expected=str(record["title"]), previous_xml=current_xml
                    )
                    return child_path, action, f"{folder_ref}/{entry_index}", child_xml

                def restore():
                    nonlocal action, current_xml
                    child_title = _bookmark_title(child_xml, profile=context.app_profile)
                    action = context.ui.click(
                        "return_from_chrome_bookmark_folder",
                        _selector(context, "chrome.bookmarks.go-back"),
                        expected=_selector(context, "chrome.screen.bookmarks"),
                        context={"folder_path": list(path)},
                    )
                    current_xml = wait_for_bookmark_title(
                        context, child_title, expected=parent_title, previous_xml=child_xml
                    )
                with visit_scope(enter, restore) as child_task:
                    action, child_xml = yield child_task
            entry_count += len(visible) - overlap
            # Carry known metadata through a later clipped observation, including
            # when child-folder records are interleaved in the retained output.
            previous = [
                {key: value for key, value in entry.items() if key not in {"folder_ref", "entry_index"}}
                for entry in (entries[-len(visible):] if visible else [])
            ]
        return action, last_xml

    traversal_error = None
    try:
        source_action = _open_surface(
            context, "chrome.menu.bookmarks", "chrome.screen.bookmarks"
        )
        source_action, root_xml = _return_to_bookmark_root(context, source_action)
        source_action, _ = walk_hierarchy(((), source_action, "root", root_xml), level)
    except (Exception, KeyboardInterrupt) as error:
        traversal_error = error
    return finish_record_collection(
        context, attempt_id, "chrome.bookmarks", records, source_action,
        surface=surface, prefix="chrome/bookmarks", record_kind="chrome_bookmarks",
        retain_name="retain_chrome_records", error=traversal_error,
        completion="recursive_bookmarks_exhausted", success_status=AcquisitionStatus.ACQUIRED,
    )


def identification_contracts(profile):
    return bind_parsing_contracts(profile, {
        'chrome.account': ('chrome.parse_account', {'row_resource_id': 'account_management_account_row', 'fields': ['android:id/title', 'android:id/summary']}),
        'chrome.history': ('chrome.parse_history', {'row_clickable': True, 'required_child_resource_ids': ['title', 'description'], 'date_marker': 'history_list_unlabelled_text', 'window_merge': 'longest_adjacent_ordered_record_overlap'}),
        'chrome.downloads': ('chrome.parse_downloads', {'row_clickable': True, 'title_sources': ['title.text', 'thumbnail.content-desc'], 'optional_child_resource_ids': ['caption'], 'date_resource_id': 'date', 'window_merge': 'longest_ordered_overlap_with_missing_field_enrichment'}),
        'chrome.bookmarks': ('chrome.parse_bookmarks', {'identity_fields': ['folder_ref', 'entry_index'], 'traverse_folders': True, 'window_merge': 'longest_adjacent_ordered_record_overlap'}),
        'chrome.recent_tabs': ('chrome.parse_recent_tabs', {'row_resource_id': 'recent_tabs_list_item_layout', 'required_child_resource_id': 'title_row', 'group_resource_id': 'recent_tabs_group_view'}),
    })


def collect(context) -> Outcome:
    context.app_profile.require_identification_rules(identification_contracts(context.app_profile))
    condition = context.base_context.get("condition", {})
    target = condition.get("target") if isinstance(condition, dict) else None
    if (
        not isinstance(target, dict)
        or target.get("kind") != "browser"
        or target.get("ref") != "browser.chrome.all"
    ):
        return Outcome(OutcomeStatus.FAILED, "chrome_target_invalid")
    if not context.start_app():
        return Outcome(OutcomeStatus.FAILED, "chrome_app_start_not_verified")

    counts: dict[str, int] = {}
    counts["history"], action = _collect_list(
        context,
        surface="history",
        target_id="chrome.history",
        item_type="history_collection",
        menu_item="chrome.menu.history",
        marker="chrome.screen.history",
        parser=partial(parse_history, profile=context.app_profile),
    )
    counts["downloads"], action = _collect_list(
        context,
        surface="downloads",
        target_id="chrome.downloads",
        item_type="download_collection",
        menu_item="chrome.menu.downloads",
        marker="chrome.screen.downloads",
        parser=partial(parse_downloads, profile=context.app_profile),
    )
    counts["bookmarks"], action = _collect_bookmarks(context)
    counts["recent_tabs"], action = _collect_list(
        context,
        surface="recent-tabs",
        target_id="chrome.recent_tabs",
        item_type="recent_tab_collection",
        menu_item="chrome.menu.recent-tabs",
        marker="chrome.screen.recent-tabs",
        parser=partial(parse_recent_tabs, profile=context.app_profile),
    )
    counts["account"], action = _collect_list(
        context,
        surface="account",
        target_id="chrome.account",
        item_type="account_context",
        menu_item="chrome.menu.settings",
        marker="chrome.screen.settings",
        parser=partial(_parse_account_list, profile=context.app_profile),
        scroll=False,
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
    "merge_windows",
    "parse_bookmarks",
    "parse_account",
    "parse_downloads",
    "parse_history",
    "parse_recent_tabs",
    "wait_for_bookmark_title",
    "collect",
]
